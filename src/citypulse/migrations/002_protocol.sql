-- Which collection path a sensor reports through: plain HTTP push, OTLP, or an industrial protocol terminated at an edge gateway.
ALTER TABLE sensor ADD COLUMN protocol text NOT NULL DEFAULT 'http' CHECK (protocol ~ '^[a-z0-9_]{1,24}$');
