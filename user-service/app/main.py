import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import Base, engine
from app.models import User, UserRole
from app.routers import auth, users
from app.security import hash_password


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

HTTP_REQUESTS = Counter(
    "user_service_http_requests_total",
    "User service HTTP requests.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "user_service_http_request_duration_seconds",
    "User service HTTP request duration in seconds.",
    ["method", "route"],
)


def initialise_database() -> None:
    maximum_attempts = 10
    retry_delay_seconds = 5

    for attempt in range(1, maximum_attempts + 1):
        try:
            Base.metadata.create_all(bind=engine)

            logger.info(
                "Database connection established successfully."
            )

            return

        except OperationalError:
            logger.warning(
                "Database connection failed. Attempt %s of %s.",
                attempt,
                maximum_attempts,
            )

            if attempt == maximum_attempts:
                logger.exception(
                    "Unable to connect to the database."
                )
                raise

            time.sleep(retry_delay_seconds)


def create_default_admin() -> None:
    admin_username = os.getenv(
        "DEFAULT_ADMIN_USERNAME",
        "admin",
    )

    admin_email = os.getenv(
        "DEFAULT_ADMIN_EMAIL",
        "admin@koalatech.edu.au",
    )

    admin_password = os.getenv(
        "DEFAULT_ADMIN_PASSWORD",
        "AdminPassword123!",
    )

    with Session(engine) as db:
        existing_admin = db.scalar(
            select(User).where(
                User.username == admin_username
            )
        )

        if existing_admin is not None:
            logger.info(
                "Default administrator account already exists."
            )
            return

        admin = User(
            username=admin_username,
            email=admin_email,
            hashed_password=hash_password(admin_password),
            role=UserRole.ADMIN,
            is_active=True,
        )

        db.add(admin)
        db.commit()

        logger.info(
            "Default administrator account created."
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialise_database()
    create_default_admin()

    yield


app = FastAPI(
    title="KoalaTech University User Service",
    description=(
        "Manages user accounts, authentication and "
        "role-based access for KoalaTech University."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


app.include_router(auth.router)
app.include_router(users.router)


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    started = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        method = request.method if request.method in {
            "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"
        } else "OTHER"
        HTTP_REQUESTS.labels(method, route, status).inc()
        HTTP_REQUEST_DURATION.labels(method, route).observe(
            time.perf_counter() - started
        )


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get(
    "/",
    tags=["Health"],
)
def root() -> dict[str, str]:
    return {
        "message": "KoalaTech University User Service is running."
    }


@app.get(
    "/health",
    tags=["Health"],
)
def health_check() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "user-service",
    }