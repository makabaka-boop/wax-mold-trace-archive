from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .database import engine, Base
from .routers import auth, users, styles, wax_batches, molds, stations, inspection_cycles, batches, inspection, dashboard, warnings, delivery_archives, reworks

Base.metadata.create_all(bind=engine)

app = FastAPI(title="手作蜡模试制流程追踪与质检归档 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": message,
            "data": None
        }
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
        msg = first.get("msg", "参数校验失败")
        message = f"{loc}：{msg}" if loc else msg
    else:
        message = "参数校验失败"
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "message": message,
            "data": None
        }
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "data": None
        }
    )


app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(users.router, prefix=settings.API_V1_PREFIX)
app.include_router(styles.router, prefix=settings.API_V1_PREFIX)
app.include_router(wax_batches.router, prefix=settings.API_V1_PREFIX)
app.include_router(molds.router, prefix=settings.API_V1_PREFIX)
app.include_router(stations.router, prefix=settings.API_V1_PREFIX)
app.include_router(inspection_cycles.router, prefix=settings.API_V1_PREFIX)
app.include_router(batches.router, prefix=settings.API_V1_PREFIX)
app.include_router(inspection.router, prefix=settings.API_V1_PREFIX)
app.include_router(dashboard.router, prefix=settings.API_V1_PREFIX)
app.include_router(warnings.router, prefix=settings.API_V1_PREFIX)
app.include_router(delivery_archives.router, prefix=settings.API_V1_PREFIX)
app.include_router(reworks.router, prefix=settings.API_V1_PREFIX)


@app.get("/")
def root():
    return {"code": 200, "message": "success", "data": {"service": "手作蜡模试制流程追踪与质检归档 API", "status": "running"}}


@app.get("/health")
def health_check():
    return {"code": 200, "message": "success", "data": {"status": "healthy"}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=True
    )
