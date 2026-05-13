import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from database import create_tables
from api.jobs import router as jobs_router
from api.clips import router as clips_router
from api.auth import router as auth_router
from api.admin import router as admin_router

load_dotenv()

app = FastAPI(title="ClipForge API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage_path = os.getenv("STORAGE_PATH", "./storage")
os.makedirs(storage_path, exist_ok=True)

app.mount("/storage", StaticFiles(directory=storage_path), name="storage")

app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(clips_router, prefix="/api")


@app.on_event("startup")
def on_startup():
    create_tables()


@app.get("/health")
def health():
    return {"status": "ok"}
