import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.health import router as health_router
from app.routes.training import router as training_router

load_dotenv()

app = FastAPI(title="EduBotics Cloud Training API")

allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(health_router)
app.include_router(training_router)
