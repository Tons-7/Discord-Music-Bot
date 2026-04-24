from fastapi import APIRouter

from .auth_routes import router as auth_router
from .state_routes import router as state_router
from .playback_routes import router as playback_router
from .queue_routes import router as queue_router
from .search_routes import router as search_router
from .favorites_routes import router as favorites_router
from .ws_routes import router as ws_router
from .config_routes import router as config_router
from .stream_routes import router as stream_router
from .image_proxy import router as image_proxy_router
from .playlist_routes import router as playlist_router
from .lyrics_routes import router as lyrics_router
from .stats_routes import router as stats_router

api_router = APIRouter()
api_router.include_router(config_router)
api_router.include_router(auth_router)
api_router.include_router(state_router)
api_router.include_router(playback_router)
api_router.include_router(queue_router)
api_router.include_router(search_router)
api_router.include_router(favorites_router)
api_router.include_router(ws_router)
api_router.include_router(stream_router)
api_router.include_router(image_proxy_router)
api_router.include_router(playlist_router)
api_router.include_router(lyrics_router)
api_router.include_router(stats_router)
