#!/bin/sh
# Create the application bucket and enable versioning. Idempotent — safe to
# re-run on every `docker compose up`.
set -eu

mc alias set local "${MINIO_ENDPOINT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"

if mc ls "local/${MINIO_BUCKET}" >/dev/null 2>&1; then
    echo "bucket ${MINIO_BUCKET} already exists"
else
    mc mb "local/${MINIO_BUCKET}"
    echo "created bucket ${MINIO_BUCKET}"
fi

# Versioning gives us recovery from an accidental overwrite of a stored document.
mc version enable "local/${MINIO_BUCKET}"

echo "minio initialisation complete"
