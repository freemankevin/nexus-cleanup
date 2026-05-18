import requests
from requests.auth import HTTPBasicAuth
import json
import re
from datetime import datetime
import logging
import time
import os
import sys
from typing import Dict, List, Optional, Tuple, Any
import schedule
from fastapi import FastAPI, Response
import uvicorn
import psutil
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    nexus_url: str = Field(default="http://nexus:8081", alias="NEXUS_URL")
    nexus_user: str = Field(default="admin", alias="NEXUS_USER")
    nexus_pass: str = Field(default="admin123", alias="NEXUS_PASS")
    repository: str = Field(default="maven-snapshots", alias="REPOSITORY_NAME")
    retain_count: int = Field(default=3, alias="RETAIN_COUNT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    retry_delay: int = Field(default=5, alias="RETRY_DELAY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    schedule_time: str = Field(default="03:00", alias="SCHEDULE_TIME")
    healthcheck_port: int = Field(default=8000, alias="HEALTHCHECK_PORT")
    delete_workers: int = Field(default=5, alias="DELETE_WORKERS")

    class Config:
        case_sensitive = False


settings = Settings()

# Global state
last_run_time = None
last_run_status = "never_run"
shutdown_event = threading.Event()

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(settings.log_level)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

file_handler = logging.FileHandler("/var/log/nexus_cleanup.log")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Reusable HTTP session
session = requests.Session()
session.auth = HTTPBasicAuth(settings.nexus_user, settings.nexus_pass)

# Healthcheck app
app = FastAPI()
SNAPSHOT_PATTERN = re.compile(r"^(.*?)-(\d{8}\.\d{6})-(\d+)$")


class NexusAPIError(Exception):
    pass


def make_api_request(method: str, url: str, **kwargs) -> requests.Response:
    for attempt in range(settings.max_retries):
        try:
            response = session.request(method, url, timeout=60, **kwargs)
            if response.status_code == 204:
                return response
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < settings.max_retries - 1:
                logger.warning(
                    f"Attempt {attempt + 1} failed: {e}. Retrying in {settings.retry_delay}s..."
                )
                time.sleep(settings.retry_delay)
                continue
            raise NexusAPIError(
                f"API request failed after {settings.max_retries} attempts: {e}"
            )


@app.get("/health")
def health_check():
    checks = {
        "storage": {
            "log_file_writable": os.access("/var/log/nexus_cleanup.log", os.W_OK),
            "disk_space": psutil.disk_usage("/").free > 100 * 1024 * 1024,
        },
        "connectivity": {
            "nexus_reachable": check_nexus_connectivity(),
        },
        "application": {
            "last_run_time": last_run_time,
            "last_run_status": last_run_status,
            "schedule": settings.schedule_time,
        },
    }
    storage_ok = all(checks["storage"].values())
    connectivity_ok = all(checks["connectivity"].values())
    app_ok = last_run_status in ("success", "never_run")
    is_healthy = storage_ok and connectivity_ok and app_ok
    status_code = 200 if is_healthy else 503
    return Response(
        content=json.dumps(
            {
                "status": "healthy" if is_healthy else "unhealthy",
                "checks": checks,
                "version": os.getenv("VERSION", "unknown"),
                "timestamp": datetime.now().isoformat(),
            }
        ),
        status_code=status_code,
        media_type="application/json",
    )


def check_nexus_connectivity() -> bool:
    try:
        response = session.get(
            f"{settings.nexus_url}/service/rest/v1/status", timeout=5
        )
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Nexus connectivity check failed: {e}")
        return False


def parse_snapshot_version(version_str: str) -> Optional[Tuple[str, datetime, int]]:
    match = SNAPSHOT_PATTERN.match(version_str)
    if match:
        base_version = match.group(1) + "-SNAPSHOT"
        timestamp_str = match.group(2)
        build_number = int(match.group(3))
        try:
            dt_obj = datetime.strptime(timestamp_str, "%Y%m%d.%H%M%S")
            return base_version, dt_obj, build_number
        except ValueError:
            return None
    return None


def get_all_components_paginated() -> Optional[List[Dict[str, Any]]]:
    all_components = []
    continuation_token = None
    url = f"{settings.nexus_url}/service/rest/v1/components"
    logger.info("Fetching components...")
    page_num = 1
    while True:
        params = {"repository": settings.repository, "version": "*-SNAPSHOT"}
        if continuation_token:
            params["continuationToken"] = continuation_token
        try:
            logger.info(f"Fetching page {page_num}...")
            response = make_api_request("GET", url, params=params)
            data = response.json()
            items = data.get("items", [])
            all_components.extend(items)
            logger.info(
                f"Found {len(items)} components on page {page_num}. Total: {len(all_components)}"
            )
            continuation_token = data.get("continuationToken")
            if not continuation_token:
                logger.info("Reached end of paginated results.")
                break
            page_num += 1
        except (NexusAPIError, json.JSONDecodeError) as e:
            logger.error(f"Failed to fetch components: {e}")
            return None
    logger.info(f"Finished fetching components. Total: {len(all_components)}")
    return all_components


def delete_component(component: Dict[str, Any]) -> Tuple[str, bool]:
    comp_id = component["id"]
    version = component.get("version", "unknown")
    url = f"{settings.nexus_url}/service/rest/v1/components/{comp_id}"
    try:
        response = make_api_request("DELETE", url)
        if response.status_code == 204:
            logger.info(f"Successfully deleted {version}")
            return comp_id, True
        return comp_id, False
    except NexusAPIError as e:
        logger.error(f"Delete failed for {version}: {e}")
        return comp_id, False


def process_snapshots(components: List[Dict[str, Any]]) -> bool:
    if not components:
        return True
    artifacts: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    success = True
    for comp in components:
        key = (comp["group"], comp["name"])
        artifacts.setdefault(key, []).append(comp)
    logger.info(f"\nProcessing {len(artifacts)} unique artifacts...")
    for (group, name), comps in artifacts.items():
        logger.info(f"\nProcessing Artifact: {group}:{name}")
        version_branches: Dict[str, List[Dict[str, Any]]] = {}
        for comp in comps:
            parsed = parse_snapshot_version(comp["version"])
            if parsed:
                base_version, dt_obj, build_number = parsed
                version_branches.setdefault(base_version, []).append(
                    {"component": comp, "datetime": dt_obj, "build": build_number}
                )
        for base_version, snapshots in version_branches.items():
            logger.info(
                f"Processing SNAPSHOT branch: {base_version} ({len(snapshots)} versions)"
            )
            snapshots.sort(key=lambda x: (x["datetime"], x["build"]), reverse=True)
            if len(snapshots) > settings.retain_count:
                to_delete = snapshots[settings.retain_count :]
                logger.info(f"Marking {len(to_delete)} for deletion.")
                if not settings.dry_run:
                    with ThreadPoolExecutor(
                        max_workers=settings.delete_workers
                    ) as executor:
                        futures = {
                            executor.submit(delete_component, item["component"]): item
                            for item in to_delete
                        }
                        for future in as_completed(futures):
                            comp_id, ok = future.result()
                            if not ok:
                                success = False
    return success


def cleanup_job():
    global last_run_time, last_run_status
    start_time = time.time()
    last_run_time = datetime.now().isoformat()
    logger.info("Starting scheduled cleanup job")
    try:
        components = get_all_components_paginated()
        if components is not None:
            if process_snapshots(components):
                last_run_status = "success"
            else:
                last_run_status = "partial_failure"
        else:
            last_run_status = "failed"
    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)
        last_run_status = "failed"
    finally:
        logger.info(
            f"Cleanup job completed in {time.time() - start_time:.2f}s (Status: {last_run_status})"
        )


def run_scheduler():
    def handle_signal(signum, _frame):
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if settings.schedule_time.lower() == "manual":
        logger.info("Running manual cleanup job")
        cleanup_job()
    else:
        logger.info(f"Scheduling daily cleanup at {settings.schedule_time}")
        schedule.every().day.at(settings.schedule_time).do(cleanup_job)
        while not shutdown_event.is_set():
            schedule.run_pending()
            shutdown_event.wait(60)


def main() -> None:
    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": app,
            "host": "0.0.0.0",
            "port": settings.healthcheck_port,
            "log_level": "error",
        },
        daemon=True,
    )
    server_thread.start()
    run_scheduler()


if __name__ == "__main__":
    main()
