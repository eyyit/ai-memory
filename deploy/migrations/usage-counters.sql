BEGIN;
CREATE TABLE memory_usage_hourly (
  instance_id varchar(128) NOT NULL,
  hour timestamptz NOT NULL,
  operation varchar(16) NOT NULL,
  traffic varchar(16) NOT NULL,
  transport varchar(16) NOT NULL,
  outcome varchar(16) NOT NULL,
  calls bigint NOT NULL,
  results bigint NOT NULL,
  duration_sum double precision NOT NULL,
  duration_max double precision NOT NULL,
  PRIMARY KEY (instance_id, hour, operation, traffic, transport, outcome)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON memory_usage_hourly TO ai_memory;
COMMIT;
