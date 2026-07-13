import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from requests.exceptions import RequestException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "http://127.0.0.1:8000"
CHAT_URL = f"{BASE_URL}/api/chat"
SUGGESTIONS_URL = f"{BASE_URL}/api/suggestions"

API_KEY = "test_dev_key"
HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY
}


def log_output(pipe: Any, log_func: Any) -> None:
    """Log the output from the given pipe."""
    for line in iter(pipe.readline, ""):
        log_func(line.strip())


def start_server() -> subprocess.Popen[str]:
    """Start the FastAPI server using subprocess and log its output."""
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.fast_api_app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    env = os.environ.copy()
    env["INTEGRATION_TEST"] = "TRUE"
    env["SOPHEE_API_KEY"] = API_KEY
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    threading.Thread(
        target=log_output, args=(process.stdout, logger.info), daemon=True
    ).start()
    threading.Thread(
        target=log_output, args=(process.stderr, logger.error), daemon=True
    ).start()

    return process


def wait_for_server(timeout: int = 90, interval: int = 1) -> bool:
    """Wait for the server to be ready."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{BASE_URL}/docs", timeout=10)
            if response.status_code == 200:
                logger.info("Server is ready")
                return True
        except RequestException:
            pass
        time.sleep(interval)
    logger.error(f"Server did not become ready within {timeout} seconds")
    return False


@pytest.fixture(scope="session")
def server_fixture(request: Any) -> Iterator[subprocess.Popen[str]]:
    """Pytest fixture to start and stop the server for testing."""
    logger.info("Starting server process")
    server_process = start_server()
    if not wait_for_server():
        pytest.fail("Server failed to start")
    logger.info("Server process started")

    def stop_server() -> None:
        logger.info("Stopping server process")
        server_process.terminate()
        server_process.wait()
        logger.info("Server process stopped")

    request.addfinalizer(stop_server)
    yield server_process


def test_auth_rejection(server_fixture: subprocess.Popen[str]) -> None:
    """Test that requests without API key are rejected."""
    response = requests.get(SUGGESTIONS_URL, timeout=10)
    assert response.status_code == 403


def test_chat(server_fixture: subprocess.Popen[str]) -> None:
    """Test the chat API functionality."""
    user_id = "test_user_123"
    session_id = "test_session_123"

    data = {
        "user_id": user_id,
        "session_id": session_id,
        "message": "Hi, who are you?",
    }
    response = requests.post(CHAT_URL, headers=HEADERS, json=data, timeout=60)
    assert response.status_code == 200
    resp_json = response.json()
    assert resp_json.get("status") == "success"
    assert "response" in resp_json


def test_get_suggestions(server_fixture: subprocess.Popen[str]) -> None:
    """Test the suggestions API functionality."""
    response = requests.get(SUGGESTIONS_URL, headers=HEADERS, timeout=10)
    assert response.status_code == 200
    assert "status" in response.json()
