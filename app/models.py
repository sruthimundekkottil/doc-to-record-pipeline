from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field

class LineItem(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal

class ExtractedInvoice(BaseModel):
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: Literal["INR", "USD", "EUR", "GBP"] | None
    line_items: list[LineItem]
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    field_confidence: dict[str, float] = Field(
        description="0.0-1.0 per field name; low when the value was unclear"
    )
