CREATE TABLE documents (
    id              UUID PRIMARY KEY,
    source_filename TEXT        NOT NULL,
    stored_path     TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    vendor_name     TEXT,
    invoice_number  TEXT,
    invoice_date    DATE,
    due_date        DATE,
    currency        CHAR(3),
    subtotal        NUMERIC(12,2),
    tax             NUMERIC(12,2),
    total           NUMERIC(12,2),
    confidence      NUMERIC(4,3),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT status_values
        CHECK (status IN ('received','approved','needs_review','failed'))
);

CREATE TABLE line_items (
    id          BIGSERIAL PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    line_number INT  NOT NULL,
    description TEXT,
    quantity    NUMERIC(12,3),
    unit_price  NUMERIC(12,2),
    line_total  NUMERIC(12,2)
);

CREATE TABLE processing_log (
    id            BIGSERIAL PRIMARY KEY,
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    detail        JSONB,
    duration_ms   INT,
    model         TEXT,
    prompt_tokens INT,
    output_tokens INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON documents (status);
CREATE INDEX ON documents (vendor_name, invoice_number);
CREATE INDEX ON line_items (document_id);
CREATE INDEX ON processing_log (document_id);
