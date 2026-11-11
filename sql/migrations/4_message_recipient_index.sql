CREATE INDEX IF NOT EXISTS idx_vex_msg_to
    ON vex_event ((payload->>'to')) WHERE type = 'message.sent';
