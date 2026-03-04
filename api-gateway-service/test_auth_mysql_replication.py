#!/usr/bin/env python3
import argparse
import base64
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib import error, request


@dataclass
class HttpResult:
    status: int
    body: Dict


def run_command(cmd: list[str]) -> str:
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(cmd)}\n"
            f"stdout: {completed.stdout}\n"
            f"stderr: {completed.stderr}"
        )
    return completed.stdout.strip()


def resolve_api_url(namespace: str, api_url: Optional[str]) -> str:
    if api_url:
        return api_url.rstrip("/")
    output = run_command(["minikube", "service", "api-gateway", "-n", namespace, "--url"])
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first_line:
        raise RuntimeError("Could not determine API URL from minikube service output")
    return first_line.rstrip("/")


def post_json(url: str, payload: Dict) -> HttpResult:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return HttpResult(status=resp.status, body=json.loads(raw) if raw else {})
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        parsed = json.loads(raw) if raw else {}
        return HttpResult(status=exc.code, body=parsed)
    except error.URLError as exc:
        raise ConnectionError(f"Could not connect to {url}: {exc}") from exc


def wait_for_api(api_url: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    health_url = f"{api_url}/"
    while time.time() < deadline:
        req = request.Request(url=health_url, method="GET")
        try:
            with request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def decode_jwt_payload(token: str) -> Dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
    return json.loads(payload_bytes.decode("utf-8"))


def read_user_from_mysql_pod(namespace: str, pod: str, username: str) -> Optional[Tuple[str, str, str]]:
    safe_username = username.replace("'", "''")
    sql = (
        "SELECT user_name, role, user_status "
        f"FROM users WHERE user_name='{safe_username}' LIMIT 1;"
    )
    output = run_command(
        [
            "kubectl",
            "exec",
            "-n",
            namespace,
            pod,
            "--",
            "mysql",
            "-N",
            "-B",
            "-uauth_user",
            "-pauth_password",
            "-D",
            "auth_db",
            "-e",
            sql,
        ]
    )
    if not output.strip():
        return None
    parts = output.split("\t")
    if len(parts) < 3:
        raise RuntimeError(f"Unexpected MySQL output from pod {pod}: {output}")
    return parts[0], parts[1], parts[2]


def wait_for_replication(
    namespace: str,
    username: str,
    expected_role: str,
    expected_status: str,
    timeout_seconds: int,
) -> Dict[str, Optional[Tuple[str, str, str]]]:
    pods = [
        "mysql-primary-0",
        "mysql-replica-1-0",
        "mysql-replica-2-0",
        "mysql-replica-3-0",
    ]
    deadline = time.time() + timeout_seconds
    latest: Dict[str, Optional[Tuple[str, str, str]]] = {pod: None for pod in pods}
    while time.time() < deadline:
        all_good = True
        for pod in pods:
            try:
                latest[pod] = read_user_from_mysql_pod(namespace, pod, username)
            except Exception:
                latest[pod] = None

            row = latest[pod]
            if row != (username, expected_role, expected_status):
                all_good = False
        if all_good:
            return latest
        time.sleep(2)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description="Integration test for auth MySQL replication")
    parser.add_argument("--namespace", default="hospital-h1", help="Kubernetes namespace")
    parser.add_argument("--api-url", default=None, help="API gateway base URL")
    parser.add_argument("--timeout", type=int, default=60, help="Replication wait timeout in seconds")
    args = parser.parse_args()

    try:
        api_url = resolve_api_url(args.namespace, args.api_url)
    except Exception as exc:
        print(f"[FAIL] Could not resolve API URL: {exc}")
        return 1

    username = f"replica-test-{uuid.uuid4().hex[:8]}"
    password = "Passw0rd!"
    role = "doctor"
    status = "active"

    print(f"[INFO] API URL: {api_url}")
    print(f"[INFO] Test username: {username}")

    if not wait_for_api(api_url, timeout_seconds=45):
        print(f"[FAIL] API is not reachable at {api_url}")
        print("[HINT] On Windows + Minikube Docker driver, prefer:")
        print("       kubectl port-forward -n hospital-h1 svc/api-gateway 8080:8080")
        print("       Then run with --api-url http://127.0.0.1:8080")
        return 1

    register_payload = {
        "userName": username,
        "doctorID": f"doc-{uuid.uuid4().hex[:8]}",
        "role": role,
    }
    try:
        register_res = post_json(f"{api_url}/auth/register", register_payload)
    except ConnectionError as exc:
        print(f"[FAIL] {exc}")
        print("[HINT] Keep the service tunnel open, or use port-forward as above.")
        return 1
    if register_res.status != 201:
        print(f"[FAIL] Register failed: status={register_res.status}, body={register_res.body}")
        return 1
    print("[PASS] Register endpoint returned 201")
    if register_res.body.get("userStatus") != "pending":
        print(f"[FAIL] Register status mismatch: {register_res.body}")
        return 1

    set_password_res = post_json(
        f"{api_url}/auth/set-password",
        {"userName": username, "password": password},
    )
    if set_password_res.status != 200:
        print(
            f"[FAIL] Set password failed: status={set_password_res.status}, body={set_password_res.body}"
        )
        return 1
    print("[PASS] Set password endpoint returned 200")

    login_res = post_json(
        f"{api_url}/auth/login",
        {"userName": username, "password": password},
    )
    if login_res.status != 200:
        print(f"[FAIL] Login failed: status={login_res.status}, body={login_res.body}")
        return 1

    token = login_res.body.get("access_token")
    if not token:
        print(f"[FAIL] Login response missing access_token: {login_res.body}")
        return 1

    try:
        claims = decode_jwt_payload(token)
    except Exception as exc:
        print(f"[FAIL] Could not decode JWT payload: {exc}")
        return 1

    if claims.get("role") != role:
        print(f"[FAIL] JWT role mismatch: expected={role}, got={claims.get('role')}")
        return 1
    print("[PASS] Login endpoint returned valid token with expected role")

    rows = wait_for_replication(
        namespace=args.namespace,
        username=username,
        expected_role=role,
        expected_status=status,
        timeout_seconds=args.timeout,
    )

    failed = [pod for pod, row in rows.items() if row != (username, role, status)]
    if failed:
        print("[FAIL] Replication check failed.")
        for pod, row in rows.items():
            print(f"  - {pod}: {row}")
        return 1

    print("[PASS] User row found on primary and all 3 replicas")
    for pod, row in rows.items():
        print(f"  - {pod}: {row}")
    print("[PASS] Integration test completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
