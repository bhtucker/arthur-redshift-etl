#!/usr/bin/env bash

START_NOW=`date -u +"%Y-%m-%dT%H:%M:%S"`
USER="${USER-nobody}"
DEFAULT_PREFIX="${ARTHUR_DEFAULT_PREFIX-$USER}"

if [[ $# -lt 1 || "$1" = "-h" ]]; then

    cat <<EOF

Single-shot extraction pipeline. You get to pick the arguments to 'extract' command.

Usage: `basename $0` extract_arg [extract_arg ...]

The remaining arguments will be passed to 'extract'.
The environment defaults to "$DEFAULT_PREFIX".

Example: `basename $0` dw

EOF
    exit 0

fi

set -e -u

# Verify that there is a local configuration directory
DEFAULT_CONFIG="${DATA_WAREHOUSE_CONFIG:-./config}"
if [[ ! -d "$DEFAULT_CONFIG" ]]; then
    echo "Failed to find \'$DEFAULT_CONFIG\' directory."
    echo "Make sure you are in the directory with your data warehouse setup or have DATA_WAREHOUSE_CONFIG set."
    exit 1
fi

function join_by { local IFS="$1"; shift; echo "$*"; }

PROJ_BUCKET=$( arthur.py show_value object_store.s3.bucket_name )
PROJ_ENVIRONMENT="$DEFAULT_PREFIX"

# The list of arguments must be comma-separated when passed to a step in an EMR cluster.
EXTRACT_ARGUMENTS=$(join_by ',' "$@")

START_DATE_TIME="$START_NOW"

# Verify that this bucket/environment pair is set up on s3
BOOTSTRAP="s3://$PROJ_BUCKET/$PROJ_ENVIRONMENT/bin/bootstrap.sh"
if ! aws s3 ls "$BOOTSTRAP" > /dev/null; then
    echo "Check whether the bucket \"$PROJ_BUCKET\" and folder \"$PROJ_ENVIRONMENT\" exist!"
    exit 1
fi

set -x

# Note: "key" and "value" are lower-case keywords here.
AWS_TAGS="key=user:project,value=data-warehouse key=user:sub-project,value=dw-etl"

PIPELINE_NAME="Extraction Pipeline ($PROJ_ENVIRONMENT @ $START_DATE_TIME)"
PIPELINE_DEFINITION_FILE="/tmp/pipeline_definition_${USER-nobody}_$$.json"
PIPELINE_ID_FILE="/tmp/pipeline_id_${USER-nobody}_$$.json"
trap "rm -f \"$PIPELINE_ID_FILE\"" EXIT

arthur.py render_template --prefix "$PROJ_ENVIRONMENT" extraction_pipeline > "$PIPELINE_DEFINITION_FILE"

aws datapipeline create-pipeline \
    --unique-id dw-etl-extraction-pipeline \
    --name "$PIPELINE_NAME" \
    --tags $AWS_TAGS \
    | tee "$PIPELINE_ID_FILE"

PIPELINE_ID=`jq --raw-output < "$PIPELINE_ID_FILE" '.pipelineId'`

if [[ -z "$PIPELINE_ID" ]]; then
    set +x
    echo "Failed to find pipeline id in output -- pipeline probably wasn't created. Check your VPN etc."
    exit 1
fi

aws datapipeline put-pipeline-definition \
    --pipeline-definition "file://$PIPELINE_DEFINITION_FILE" \
    --parameter-values \
        myStartDateTime="$START_DATE_TIME" \
        myExtractArguments="$EXTRACT_ARGUMENTS" \
    --pipeline-id "$PIPELINE_ID"

aws datapipeline activate-pipeline --pipeline-id "$PIPELINE_ID"

set +x
echo
echo "You can monitor the status of this extraction pipeline using:"
echo "  watch arthur.py show_pipelines -q '$PIPELINE_ID'"
