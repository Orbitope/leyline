from fastapi import Depends, FastAPI

from db import Order, get_db

app = FastAPI()


@app.get("/orders")
def list_orders(db=Depends(get_db)):
    return db.query(Order).all()
