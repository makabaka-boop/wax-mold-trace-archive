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
    # 将 HTTPException 统一为 ApiResponse{code, message, data} 结构
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 参数/数据契约校验失败（含非法状态取值），统一返回 422 的 ApiResponse
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        message = f"参数校验失败: {loc} {first.get('msg', '')}".strip()
    else:
        message = "参数校验失败"
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": message, "data": None},
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
