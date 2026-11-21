"""Notebook-friendly service lifecycle and optional Cloudflare Tunnel."""

import json
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from store import save_json

PACKAGE = Path(__file__).resolve().parent


def registry(root):
    path = Path(root) / "processes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def stop_process(pid, marker):
    """Only stop a process whose command still belongs to this application."""
    if not pid:
        return
    process_path = Path(f"/proc/{pid}/cmdline")
    if not process_path.exists() or marker not in process_path.read_bytes().decode(
        errors="replace"
    ):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(150):
            if not process_path.exists():
                return
            try:
                waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                if waited_pid:
                    return
            except ChildProcessError:
                pass
            time.sleep(0.2)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_services(root):
    root = Path(root).resolve()
    saved = registry(root)
    stop_process(saved.get("tunnel_pid"), str(root / "bin" / "cloudflared"))
    stop_process(saved.get("api_pid"), str(PACKAGE))
    save_json(root / "processes.json", {})
    print("API and tunnel stopped. Completed files remain in the job folder.")


def access_token(root):
    path = Path(root) / "api_token.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(36), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text().strip()


def start_api(root, port=8000):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stop_services(root)
    token = access_token(root)
    environment = os.environ.copy()
    environment.update(
        VIDEO_API_ROOT=str(root), VIDEO_API_TOKEN=token, PYTHONUNBUFFERED="1"
    )
    log = (root / "api.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--app-dir",
            str(PACKAGE),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    log.close()
    save_json(root / "processes.json", dict(api_pid=process.pid, port=port))
    session = requests.Session()
    session.trust_env = False
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError(
                "API exited. Read "
                + str(root / "api.log")
                + "\n"
                + (root / "api.log").read_text()[-3000:]
            )
        try:
            response = session.get(
                f"http://127.0.0.1:{port}/options",
                headers={"Authorization": f"Bearer {token}"},
                timeout=2,
            )
            if response.status_code == 200:
                return dict(local_url=f"http://127.0.0.1:{port}", api_token=token)
        except requests.RequestException:
            pass
        time.sleep(0.25)
    stop_process(process.pid, str(PACKAGE))
    raise RuntimeError(
        f"API readiness timed out. Read {root/'api.log'}. Check whether port {port} is already used."
    )


def cloudflared_binary(root):
    root = Path(root).resolve()
    binary = root / "bin" / "cloudflared"
    if binary.exists():
        subprocess.run(
            [str(binary), "--version"], check=True, capture_output=True, timeout=10
        )
        return binary
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or architecture is None:
        raise RuntimeError(
            "This installer supports Linux/Colab. Install cloudflared manually on other systems."
        )
    binary.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{architecture}"
    temporary = binary.with_suffix(".partial")
    try:
        with requests.get(url, stream=True, timeout=(15, 90)) as response:
            response.raise_for_status()
            count = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    count += len(chunk)
                    if count > 150 * 1024 * 1024:
                        raise RuntimeError("Unexpected cloudflared download size.")
                    output.write(chunk)
        temporary.chmod(0o700)
        subprocess.run(
            [str(temporary), "--version"], check=True, capture_output=True, timeout=10
        )
        temporary.replace(binary)
    finally:
        temporary.unlink(missing_ok=True)
    return binary


def start_tunnel(root, port=8000, tunnel_token="", public_base_url=""):
    root = Path(root).resolve()
    saved = registry(root)
    if not saved.get("api_pid"):
        raise RuntimeError("Start the API cell before starting the tunnel.")
    if os.environ.get("VIDEO_API_SELF_TEST") == "1":
        raise RuntimeError(
            "Turn off internal self-test mode before exposing the service."
        )
    if tunnel_token and not public_base_url.startswith("https://"):
        raise ValueError(
            "A named tunnel needs its configured https:// public_base_url."
        )
    binary = cloudflared_binary(root)
    stop_process(saved.get("tunnel_pid"), str(binary))
    environment = os.environ.copy()
    command = [str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2"]
    if tunnel_token:
        environment["TUNNEL_TOKEN"] = tunnel_token
        command += ["run"]
    else:
        command += ["--url", f"http://127.0.0.1:{port}"]
    log_path = root / "cloudflared.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    log.close()
    saved["tunnel_pid"] = process.pid
    save_json(root / "processes.json", saved)
    for _ in range(180):
        if process.poll() is not None:
            raise RuntimeError(
                "Cloudflare Tunnel exited.\n" + log_path.read_text()[-3000:]
            )
        match = re.search(
            r"https://[a-z0-9-]+\.trycloudflare\.com", log_path.read_text()
        )
        registered = "Registered tunnel connection" in log_path.read_text()
        if registered and (match or tunnel_token):
            url = public_base_url.rstrip("/") if tunnel_token else match.group()
            saved["public_url"] = url
            save_json(root / "processes.json", saved)
            return url
        time.sleep(0.5)
    stop_process(process.pid, str(binary))
    raise RuntimeError(
        f"Tunnel URL was not available after 90 seconds. Read {log_path}."
    )
