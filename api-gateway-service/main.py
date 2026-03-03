import os
import asyncio
import time
import grpc
import grpc.aio
from typing import List
from fastapi import FastAPI, HTTPException, Query, Depends, status
from grpc_client import GrpcClient
from grpc_cluster_client import GrpcClusterClient
from models import (
    PatientCreate,
    PatientUpdate,
    PatientResponse,
    DeleteResponse,
    ErrorResponse
)
from p2p_client import P2PCommandClient
from p2p_cluster_client import P2PClusterClient
from auth.auth import require_doctor, require_patient, require_doctor_or_patient
from auth.routes import router as auth_router
from auth.mysql_store import initialize_schema as initialize_auth_schema

# Initialize FastAPI app
app = FastAPI(
    title="EHR API Gateway",
    description="REST API Gateway for Distributed EHR System using gRPC",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.include_router(auth_router)

# Configuration from environment variables
# Legacy single-host mode (for local development)
GRPC_HOST = os.getenv('GRPC_HOST', 'localhost')
GRPC_PORT = int(os.getenv('GRPC_PORT', '50051'))
P2P_HOST = os.getenv('P2P_HOST', 'localhost')
P2P_PORT = int(os.getenv('P2P_PORT', '7001'))

# Cluster mode configuration (for Kubernetes)
P2P_SERVICE_NAME = os.getenv('P2P_SERVICE_NAME')  # e.g., 'hospital-headless'
NAMESPACE = os.getenv('NAMESPACE')  # e.g., 'hospital-h1'
STATEFULSET_NAME = os.getenv('STATEFULSET_NAME', 'hospital')
HOSPITAL_REPLICAS = int(os.getenv('HOSPITAL_REPLICAS', '3'))

# Common settings
API_HOST = os.getenv('API_HOST', '0.0.0.0')
API_PORT = int(os.getenv('API_PORT', '8080'))
P2P_TIMEOUT = float(os.getenv('P2P_TIMEOUT', '5.0'))
GRPC_TIMEOUT = float(os.getenv('GRPC_TIMEOUT', '5.0'))
AUTH_DB_REQUIRED = os.getenv('AUTH_DB_REQUIRED', 'false').lower() == 'true'
AUTH_DB_INIT_MAX_ATTEMPTS = int(os.getenv('AUTH_DB_INIT_MAX_ATTEMPTS', '30'))
AUTH_DB_INIT_RETRY_DELAY_SECONDS = float(os.getenv('AUTH_DB_INIT_RETRY_DELAY_SECONDS', '2.0'))

# Determine if running in cluster mode (TRUE WHEN DEPLOYED IN KUBERNETES)
CLUSTER_MODE = P2P_SERVICE_NAME is not None and NAMESPACE is not None

print("=" * 60)
print("API Gateway Configuration")
print("=" * 60)
print(f"Mode: {'CLUSTER (Kubernetes)' if CLUSTER_MODE else 'SINGLE-HOST (Legacy)'}")
if CLUSTER_MODE:
    print(f"Namespace: {NAMESPACE}")
    print(f"Service: {P2P_SERVICE_NAME}")
    print(f"StatefulSet: {STATEFULSET_NAME}")
    print(f"Replicas: {HOSPITAL_REPLICAS}")
    print(f"P2P Port: {P2P_PORT}")
    print(f"gRPC Port: {GRPC_PORT}")
else:
    print(f"P2P Server: {P2P_HOST}:{P2P_PORT}")
    print(f"gRPC Server: {GRPC_HOST}:{GRPC_PORT}")
print("=" * 60)


@app.on_event("startup")
def startup_event():
    last_error = None
    for attempt in range(1, AUTH_DB_INIT_MAX_ATTEMPTS + 1):
        try:
            initialize_auth_schema()
            print(f"Auth MySQL schema initialized (attempt {attempt})")
            return
        except Exception as exc:
            last_error = exc
            if attempt == AUTH_DB_INIT_MAX_ATTEMPTS:
                break
            print(
                f"Auth MySQL init attempt {attempt}/{AUTH_DB_INIT_MAX_ATTEMPTS} failed: {exc}. "
                f"Retrying in {AUTH_DB_INIT_RETRY_DELAY_SECONDS}s..."
            )
            time.sleep(AUTH_DB_INIT_RETRY_DELAY_SECONDS)

    if AUTH_DB_REQUIRED:
        raise RuntimeError(
            f"Auth MySQL initialization failed after {AUTH_DB_INIT_MAX_ATTEMPTS} attempts"
        ) from last_error

    print(
        f"Auth MySQL initialization skipped after {AUTH_DB_INIT_MAX_ATTEMPTS} attempts: {last_error}"
    )


@app.get("/", tags=["Health"])
async def root():
    """Root endpoint - API health check"""
    if CLUSTER_MODE:
        return {
            "message": "EHR API Gateway is running",
            "version": "1.0.0",
            "mode": "cluster",
            "namespace": NAMESPACE,
            "service": P2P_SERVICE_NAME,
            "replicas": HOSPITAL_REPLICAS
        }
    else:
        return {
            "message": "EHR API Gateway is running",
            "version": "1.0.0",
            "mode": "single-host",
            "grpc_server": f"{GRPC_HOST}:{GRPC_PORT}",
            "p2p_server": f"{P2P_HOST}:{P2P_PORT}"
        }


def get_p2p_client():
    """Factory function to get appropriate P2P client based on mode"""
    if CLUSTER_MODE:
        return P2PClusterClient(
            service_name=P2P_SERVICE_NAME,
            namespace=NAMESPACE,
            statefulset_name=STATEFULSET_NAME,
            replicas=HOSPITAL_REPLICAS,
            port=P2P_PORT,
            timeout_s=P2P_TIMEOUT
        )
    else:
        return P2PCommandClient(P2P_HOST, P2P_PORT, P2P_TIMEOUT)


def get_grpc_client():
    """Factory function to get appropriate gRPC client based on mode"""
    if CLUSTER_MODE:
        return GrpcClusterClient(
            service_name=P2P_SERVICE_NAME,
            namespace=NAMESPACE,
            statefulset_name=STATEFULSET_NAME,
            replicas=HOSPITAL_REPLICAS,
            port=GRPC_PORT,
            timeout_s=GRPC_TIMEOUT
        )
    else:
        return GrpcClient(GRPC_HOST, GRPC_PORT)


@app.post(
    "/patients",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Patients"],
    summary="Create a new patient",
    responses={
        201: {"description": "Patient created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid input"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def create_patient(patient: PatientCreate, user=Depends(require_doctor)):
    """
    Create a new patient record.

    - **patientId**: Unique patient identifier (e.g., P-2026-001)
    - **identity**: Patient identity information (patientId, mrn, nationalId)
    - **demographics**: Name, date of birth, sex, gender, deceased status
    - **contacts**: Address, phone, email
    - **sourceHospital**: Name of the hospital node creating the record
    """

    try:
        patient_data = patient.model_dump()
        async with get_p2p_client() as p2p:
            response = await p2p.submit_command("PATIENT_CREATE", patient_data)
            if not response.accepted:
                raise HTTPException(
                    status_code=503,
                    detail=f"Raft leader unavailable. leader_id={response.leader_id}"
                )
            if not response.committed:
                raise HTTPException(
                    status_code=503,
                    detail="Command accepted but not committed"
                )

        # Retry logic: Wait for the database to be updated after Raft commit
        # The Raft commit listener applies the change asynchronously
        max_retries = 5
        retry_delay = 0.1  # 100ms between retries

        for attempt in range(max_retries):
            try:
                async with get_grpc_client() as client:
                    result = await client.search_patient_by_id(patient.identity.patientId)
                    return PatientResponse(**result)
            except grpc.aio.AioRpcError as e:
                if e.code() == grpc.StatusCode.NOT_FOUND and attempt < max_retries - 1:
                    # Patient not found yet, wait and retry
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    # Re-raise on last attempt or other errors
                    raise

    except grpc.aio.AioRpcError as e:
        if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            raise HTTPException(status_code=400, detail=e.details())
        elif e.code() == grpc.StatusCode.ALREADY_EXISTS:
            raise HTTPException(status_code=409, detail=e.details())
        elif e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(
                status_code=500,
                detail=f"Patient creation confirmed by Raft but not found in database. This might indicate a replication delay. Patient ID: {patient.identity.patientId}"
            )
        else:
            raise HTTPException(
                status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/patients/{patient_uuid}",
    response_model=PatientResponse,
    tags=["Patients"],
    summary="Get patient by UUID",
    responses={
        200: {"description": "Patient found"},
        404: {"model": ErrorResponse, "description": "Patient not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def get_patient(patient_uuid: str, user=Depends(require_doctor_or_patient)):
    """
    Retrieve a patient by their UUID.

    - **patient_uuid**: The unique UUID of the patient
    """
    if user["role"] == "patient" and user["patient_uuid"] != patient_uuid:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        async with get_grpc_client() as client:
            result = await client.get_patient(patient_uuid)
            return PatientResponse(**result)
    except grpc.aio.AioRpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            raise HTTPException(status_code=400, detail=e.details())
        else:
            raise HTTPException(
                status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/patients",
    response_model=List[PatientResponse],
    tags=["Patients"],
    summary="Get all patients",
    responses={
        200: {"description": "List of patients"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def get_all_patients(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000,
                       description="Maximum number of records to return"),
    user=Depends(require_doctor)
):
    """
    Retrieve all patients with pagination.

    - **skip**: Number of records to skip (default: 0)
    - **limit**: Maximum number of records to return (default: 100, max: 1000)
    """
    try:
        async with get_grpc_client() as client:
            results = await client.get_all_patients(skip=skip, limit=limit)
            return [PatientResponse(**r) for r in results]
    except grpc.aio.AioRpcError as e:
        raise HTTPException(
            status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/patients/search/{patient_id}",
    response_model=PatientResponse,
    tags=["Patients"],
    summary="Search patient by patient ID",
    responses={
        200: {"description": "Patient found"},
        404: {"model": ErrorResponse, "description": "Patient not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def search_patient_by_id(patient_id: str, user=Depends(require_doctor_or_patient)):
    """
    Search for a patient by their patient_id.

    - **patient_id**: The patient identifier (e.g., P001)
    - Doctors can search any patient. Patients can only search their own record.
    """
    if user["role"] == "patient":
        token_patient_id = user.get("patient_id")
        if token_patient_id != patient_id:
            raise HTTPException(
                status_code=403,
                detail="Access denied: you can only access your own patient record"
            )

    try:
        async with get_grpc_client() as client:
            result = await client.search_patient_by_id(patient_id)
            return PatientResponse(**result)
    except grpc.aio.AioRpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        else:
            raise HTTPException(
                status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put(
    "/patients/{patient_id}",
    response_model=PatientResponse,
    tags=["Patients"],
    summary="Update patient",
    responses={
        200: {"description": "Patient updated successfully"},
        404: {"model": ErrorResponse, "description": "Patient not found"},
        400: {"model": ErrorResponse, "description": "Invalid input"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def update_patient(patient_id: str, patient: PatientUpdate, user=Depends(require_doctor_or_patient)):
    """
    Update a patient's information.

    - **patient_id**: The business patient ID (e.g., P-2026-030)
    - All fields are optional - only provided fields will be updated
    - Doctors can update any patient. Patients can only update their own record.
    """
    if user["role"] == "patient":
        token_patient_id = user.get("patient_id")
        if token_patient_id != patient_id:
            raise HTTPException(
                status_code=403,
                detail="Access denied: you can only update your own patient record"
            )
        # Patients may only update contact info (address, phone, email)
        if patient.demographics is not None:
            raise HTTPException(
                status_code=403,
                detail="Access denied: patients cannot update demographics"
            )
        if patient.conditions is not None:
            raise HTTPException(
                status_code=403,
                detail="Access denied: patients cannot update conditions"
            )

    try:
        # Only include fields that are not None
        patient_data = patient.model_dump(exclude_none=True)
        if not patient_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Resolve patient_id (business ID) → patient_uuid (MongoDB UUID)
        try:
            async with get_grpc_client() as client:
                existing = await client.search_patient_by_id(patient_id)
                patient_uuid = existing["id"]
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=404,
                    detail=f"Patient with ID {patient_id} not found"
                )
            raise

        # Submit update command to Raft
        async with get_p2p_client() as p2p:
            response = await p2p.submit_command(
                "PATIENT_UPDATE",
                {"patient_uuid": patient_uuid, "update": patient_data},
            )
            if not response.accepted:
                raise HTTPException(
                    status_code=503,
                    detail=f"Raft leader unavailable. leader_id={response.leader_id}"
                )
            if not response.committed:
                raise HTTPException(
                    status_code=503,
                    detail="Command accepted but not committed"
                )

        # Post-validation: Retry logic to wait for the database to be updated after Raft commit
        max_retries = 5
        retry_delay = 0.1  # 100ms between retries

        for attempt in range(max_retries):
            try:
                async with get_grpc_client() as client:
                    result = await client.get_patient(patient_uuid)
                    return PatientResponse(**result)
            except grpc.aio.AioRpcError as e:
                if e.code() == grpc.StatusCode.NOT_FOUND:
                    # Patient was deleted between pre-validation and post-validation
                    # or update failed to apply
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        raise HTTPException(
                            status_code=500,
                            detail=f"Update command was committed but patient no longer exists in database"
                        )
                else:
                    # Re-raise other errors
                    raise

    except HTTPException:
        raise
    except grpc.aio.AioRpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            raise HTTPException(status_code=400, detail=e.details())
        else:
            raise HTTPException(
                status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete(
    "/patients/{patient_uuid}",
    response_model=DeleteResponse,
    tags=["Patients"],
    summary="Delete patient",
    responses={
        200: {"description": "Patient deleted successfully"},
        404: {"model": ErrorResponse, "description": "Patient not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    }
)
async def delete_patient(patient_uuid: str, user=Depends(require_doctor)):
    """
    Delete a patient record.

    - **patient_uuid**: The unique UUID of the patient
    """
    try:
        # First, verify the patient exists before attempting deletion
        try:
            async with get_grpc_client() as client:
                await client.get_patient(patient_uuid)
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                raise HTTPException(
                    status_code=404,
                    detail=f"Patient with UUID {patient_uuid} not found"
                )
            raise

        # Submit delete command to Raft
        async with get_p2p_client() as p2p:
            response = await p2p.submit_command(
                "PATIENT_DELETE",
                {"patient_uuid": patient_uuid},
            )
            if not response.accepted:
                raise HTTPException(
                    status_code=503,
                    detail=f"Raft leader unavailable. leader_id={response.leader_id}"
                )
            if not response.committed:
                raise HTTPException(
                    status_code=503,
                    detail="Command accepted but not committed"
                )

        # Verify deletion succeeded by checking if patient no longer exists
        # Wait a bit for the delete to propagate through Raft commit listeners
        await asyncio.sleep(0.2)  # 200ms for commit listeners to apply

        try:
            async with get_grpc_client() as client:
                await client.get_patient(patient_uuid)
                # If we get here, patient still exists - deletion failed
                raise HTTPException(
                    status_code=500,
                    detail="Delete command was committed but patient still exists in database"
                )
        except grpc.aio.AioRpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                # Perfect! Patient is gone, deletion succeeded
                return DeleteResponse(success=True, message="Patient deleted successfully")
            else:
                # Some other error occurred
                raise HTTPException(
                    status_code=500,
                    detail=f"Error verifying deletion: {e.details()}"
                )

    except HTTPException:
        raise
    except grpc.aio.AioRpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=e.details())
        elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            raise HTTPException(status_code=400, detail=e.details())
        else:
            raise HTTPException(
                status_code=500, detail=f"gRPC error: {e.details()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    import uvicorn

    print("=" * 60)
    print("EHR API Gateway - Starting Server")
    print("=" * 60)
    print(f"API Documentation: http://localhost:{API_PORT}/docs")
    print(f"Alternative Docs: http://localhost:{API_PORT}/redoc")
    print(f"gRPC Backend: {GRPC_HOST}:{GRPC_PORT}")
    print("=" * 60)

    uvicorn.run(
        "main:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
        log_level="info"
    )
