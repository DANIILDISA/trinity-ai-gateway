from pydantic import BaseModel, Field


class PaymentCreateRequest(BaseModel):
    amount: int = Field(ge=1, description="Top-up amount in whole rubles")


class PaymentCreateResponse(BaseModel):
    payment_id: str
    confirmation_url: str
    status: str
    amount: str


class PaymentStatusResponse(BaseModel):
    payment_id: str
    status: str
    amount: str
