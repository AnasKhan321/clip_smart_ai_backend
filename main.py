import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from database import create_tables
from api.jobs import router as jobs_router
from api.clips import router as clips_router
from api.auth import router as auth_router
from api.admin import router as admin_router
from api.debug import router as debug_router

load_dotenv()

CORS_ORIGINS = [
"http://localhost:5173",
"http://127.0.0.1:5173",
"https://clip-smart-ai-frontend-kappa.vercel.app"
]

print(f"CORS_ORIGINS: {CORS_ORIGINS}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(title="ClipForge API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
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
app.include_router(debug_router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
