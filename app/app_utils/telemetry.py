"""OpenTelemetry configuration for GenAI tracing."""

import logging
import os

logger = logging.getLogger("sophee.app.telemetry")


def setup_telemetry():
    """Configures OpenTelemetry for GenAI tracing.

    Sets environment variables for log upload to GCS bucket,
    metadata-only mode, JSONL format. Disabled gracefully when
    no GCS bucket is configured.
    """
    bucket_name = os.getenv("LOGS_BUCKET_NAME")
    if not bucket_name:
        logger.info("LOGS_BUCKET_NAME not set, telemetry disabled")
        return

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    os.environ.setdefault(
        "ADK_TELEMETRY_LOG_FILE_PREFIX",
        f"gs://{bucket_name}/telemetry/",
    )
    os.environ.setdefault("ADK_TELEMETRY_MODE", "metadata_only")
    os.environ.setdefault("ADK_TELEMETRY_FORMAT", "jsonl")

    logger.info("Telemetry configured for bucket: %s", bucket_name)
