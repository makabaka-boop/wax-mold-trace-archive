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


# ============ 数据契约：所有接口（含错误）统一返回 ApiResponse{code,message,data} ============
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": detail, "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = []
    for error in exc.errors():
        loc = ".".join(str(x) for x in error["loc"] if x not in ("body", "query", "path"))
        msg = error["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        messages.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": "参数校验失败：" + "；".join(messages), "data": None},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {"message": "手作蜡模试制流程追踪与质检归档 API 运行正常"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=True
    )
