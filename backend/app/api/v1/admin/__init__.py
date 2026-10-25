from fastapi import APIRouter

from app.api.v1.admin.alerts import router as alerts_router
from app.api.v1.admin.audit import router as audit_router
from app.api.v1.admin.dashboard import router as dashboard_router
from app.api.v1.admin.export import router as export_router
from app.api.v1.admin.keys import router as keys_router
from app.api.v1.admin.models import router as models_router
from app.api.v1.admin.monitoring import router as monitoring_router
from app.api.v1.admin.requests import router as requests_router
from app.api.v1.admin.settings import router as settings_router
from app.api.v1.admin.storage import router as storage_router
from app.api.v1.admin.tariffs import router as tariffs_router
from app.api.v1.admin.users import router as users_router

router = APIRouter()
router.include_router(alerts_router, prefix="/alerts")
router.include_router(audit_router, prefix="/audit")
router.include_router(dashboard_router, prefix="/dashboard")
router.include_router(export_router, prefix="/export")
router.include_router(keys_router, prefix="/keys")
router.include_router(models_router, prefix="/models")
router.include_router(monitoring_router, prefix="/monitoring")
router.include_router(requests_router, prefix="/requests")
router.include_router(settings_router, prefix="/settings")
router.include_router(storage_router, prefix="/storage")
router.include_router(tariffs_router, prefix="/tariffs")
router.include_router(users_router, prefix="/users")
