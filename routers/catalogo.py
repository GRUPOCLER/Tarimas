from fastapi import APIRouter
router = APIRouter()

@router.get("/")
async def listar():
    return {"mensaje": "Catalogo HMCK — pendiente"}
