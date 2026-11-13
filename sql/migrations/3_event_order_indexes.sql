-- Composite indexes for ORDER BY ts, seq scans filtered by execution_id.
CREATE INDEX IF NOT EXISTS idx_vex_event_exec_ts ON vex_event (execution_id, ts, seq);
CREATE INDEX IF NOT EXISTS idx_vex_event_ts_seq  ON vex_event (ts, seq);
