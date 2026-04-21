import json
import io
import zipfile
import tempfile
import requests
import logging
from typing import Dict, Any, List, Optional, Tuple

from azure.identity import ClientSecretCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    PromptAgentDefinition,
    CodeInterpreterTool,
    AutoCodeInterpreterToolParam
)

SETTINGS_FILE = "settings.json"

# ====== Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("apps2_exec_report.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Settings helpers (matches your existing pattern)
# ------------------------------------------------------------------
def get_api_key(api_name: str) -> Optional[str]:
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            for api in data.get("api_keys", []):
                if api["api_name"] == api_name:
                    return api["api_key"]
        return None
    except Exception:
        logger.exception("Failed reading settings.json")
        return None


def get_section(section_name: str) -> Dict[str, Any]:
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            return data.get(section_name, {})
    except Exception:
        logger.exception("Failed reading settings.json")
        return {}


# ------------------------------------------------------------------
# Shared: build credential and project client
# ------------------------------------------------------------------
def build_credential() -> ClientSecretCredential:
    return ClientSecretCredential(
        tenant_id=get_api_key("TENANT_ID"),
        client_id=get_api_key("CLIENT_ID"),
        client_secret=get_api_key("CLIENT_SECRET")
    )


def build_project_client(project_endpoint: str) -> AIProjectClient:
    credential = build_credential()
    return AIProjectClient(endpoint=project_endpoint, credential=credential)


# ------------------------------------------------------------------
# OneVizion: get list of Report_Repository trackors from endpoint
# ------------------------------------------------------------------
def list_report_repository_trackors() -> List[Dict[str, Any]]:
    """
    Pulls all TRACKOR_ID records from the endpoint provided by the user:
    /api/v3/trackor_types/Report_Repository/trackors?fields=...&RR_AZURE_AGENT_NAME=null
    """
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")
    if not base_url or not bearer_token:
        raise Exception("Missing OneVizion base URL or bearer token")

    # If you want to make the query configurable, put it in settings.json;
    # otherwise keep the hard-coded path exactly as provided.
    path = (
        "/api/v3/trackor_types/Report_Repository/trackors"
        "?fields=RR_AZURE_AGENT_NAME%2CRR_AZURE_PROJECT_ENDPOINT"
        "&RR_AZURE_AGENT_NAME=not%20null"
    )

    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}

    logger.info("Listing Report_Repository trackors via %s", url)
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        raise Exception(f"Unexpected response format from trackor list endpoint: {type(data)}")
    return data


# ------------------------------------------------------------------
# OneVizion: download E-File using Bearer token (per-trackor)
# ------------------------------------------------------------------
def download_onevizion_efile(trackor_id: int) -> bytes:
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")

    ov_cfg = get_section("onevizion")
    field_name = ov_cfg["source_efile_field_name"]

    if not base_url or not bearer_token:
        raise Exception("Missing OneVizion base URL or bearer token")

    url = f"{base_url.rstrip('/')}/api/v3/trackor/{trackor_id}/file/{field_name}"
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/octet-stream"}

    logger.info("Downloading OneVizion E-File from %s", url)
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.content


# ------------------------------------------------------------------
# Handle Single E-File or Multi E-File (ZIP)
# ------------------------------------------------------------------
def extract_csv(file_bytes: bytes) -> Tuple[bytes, str]:
    ov_cfg = get_section("onevizion")
    preferred_csv = ov_cfg.get("csv_filename")

    if zipfile.is_zipfile(io.BytesIO(file_bytes)):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            names = z.namelist()

            if preferred_csv and preferred_csv in names:
                logger.info("Extracting preferred CSV: %s", preferred_csv)
                return z.read(preferred_csv), preferred_csv

            csvs = [n for n in names if n.lower().endswith(".csv")]
            if not csvs:
                raise Exception("ZIP contained no CSV files")

            logger.info("Extracting first CSV found: %s", csvs[0])
            return z.read(csvs[0]), csvs[0]

    return file_bytes, "onevizion_data.csv"


# ------------------------------------------------------------------
# Azure Foundry: upload file only (no agent) - endpoint is per-trackor
# ------------------------------------------------------------------
def upload_file_only(project_endpoint: str, csv_path: str) -> str:
    if not project_endpoint:
        raise Exception("PROJECT_ENDPOINT missing for this trackor")

    project_client = build_project_client(project_endpoint)
    openai_client = project_client.get_openai_client()

    with open(csv_path, "rb") as f:
        uploaded = openai_client.files.create(file=f, purpose="assistants")

    logger.info("File uploaded successfully: file_id=%s", uploaded.id)
    return uploaded.id


# ------------------------------------------------------------------
# OneVizion: Load Azure File ID to RR Text Field (per-trackor)
# ------------------------------------------------------------------
def update_trackor_with_file_id(trackor_id: int, file_id: str) -> None:
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")
    ov_cfg = get_section("onevizion")
    field_name = ov_cfg["target_file_id_field_name"]

    url = f"{base_url.rstrip('/')}/api/v3/trackors/{trackor_id}"
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    payload = {field_name: file_id}

    logger.info("Updating OneVizion Trackor %s field %s with file_id %s", trackor_id, field_name, file_id)
    resp = requests.put(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()


# ------------------------------------------------------------------
# OneVizion: Save Agent Version Number to RR Fields (per-trackor)
# ------------------------------------------------------------------
def update_trackor_with_agent_info(trackor_id: int, agent_version: int) -> None:
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")

    if not base_url or not bearer_token:
        raise Exception("Missing OneVizion base URL or bearer token")

    url = f"{base_url.rstrip('/')}/api/v3/trackors/{trackor_id}"
    headers = {"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    payload = {"RR_AGENT_VERSION": agent_version}

    resp = requests.put(url, headers=headers, json=payload, timeout=30)
    if not resp.ok:
        logger.error("Failed updating Trackor %s with agent info: %s", trackor_id, resp.text)
        resp.raise_for_status()

    logger.info("Updated OV Trackor %s with Version=%s", trackor_id, agent_version)


# ------------------------------------------------------------------
# Azure Foundry: determine Model Deployment Name from latest agent version
# ------------------------------------------------------------------
def resolve_model_and_instructions_from_latest_agent_version(
    project_client: AIProjectClient,
    agent_name: str
) -> tuple[str, str]:
    """
    Resolves BOTH:
      - model deployment name
      - instructions
    from the LATEST version of an existing agent.

    This ensures new versions inherit behavior exactly, changing
    only tools or attachments.
    """

    agents_ops = project_client.agents

    if not hasattr(agents_ops, "list_versions"):
        raise Exception(
            "This azure-ai-projects SDK version does not expose agents.list_versions; "
            "cannot auto-resolve agent configuration."
        )

    versions = list(agents_ops.list_versions(agent_name=agent_name))
    if not versions:
        raise Exception(f"No versions found for agent '{agent_name}'")

    # Determine latest version number robustly
    def vnum(v):
        return int(getattr(v, "version", v))

    latest = max(versions, key=vnum)
    latest_version_number = vnum(latest)

    # Retrieve full version details if supported
    details = latest
    if hasattr(agents_ops, "get_version"):
        details = agents_ops.get_version(
            agent_name=agent_name,
            agent_version=latest_version_number
        )

    # ---- Extract definition safely ----
    definition = None

    if hasattr(details, "definition"):
        definition = details.definition
    elif isinstance(details, dict):
        definition = details.get("definition")

    if definition is None:
        raise Exception(
            f"Unable to read definition for latest version of agent '{agent_name}'"
        )

    # ---- Extract model deployment ----
    model = getattr(definition, "model", None)
    if not model and isinstance(definition, dict):
        model = definition.get("model")

    # ---- Extract instructions ----
    instructions = getattr(definition, "instructions", None)
    if not instructions and isinstance(definition, dict):
        instructions = definition.get("instructions")

    if not model or not instructions:
        raise Exception(
            f"Latest agent version for '{agent_name}' is missing model or instructions "
            f"(model={model}, instructions_present={bool(instructions)})"
        )

    return model, instructions


# ------------------------------------------------------------------
# Azure Foundry: Attach file to EXISTING agent (create new version) - per trackor
# ------------------------------------------------------------------
def attach_file_to_existing_agent_code_interpreter(
    trackor_id: int,
    project_endpoint: str,
    agent_name: str,
    file_id: str
) -> None:
    """
    Creates a NEW VERSION of an existing agent, reusing:
      - model deployment
      - instructions
    from the latest agent version, while attaching Code Interpreter + file.
    """

    if not project_endpoint:
        raise Exception("Missing PROJECT_ENDPOINT for this trackor")
    if not agent_name:
        raise Exception("Missing agent_name for this trackor")

    project_client = build_project_client(project_endpoint)

    # ✅ Resolve BOTH model deployment and instructions from latest version
    model_deployment_name, instructions = (
        resolve_model_and_instructions_from_latest_agent_version(
            project_client=project_client,
            agent_name=agent_name
        )
    )

    code_interpreter = CodeInterpreterTool(
        container=AutoCodeInterpreterToolParam(file_ids=[file_id])
    )

    new_agent = project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=model_deployment_name,     # inherited
            instructions=instructions,       # inherited
            tools=[code_interpreter]         # only change
        ),
        description=(
            f"Auto version created to attach file {file_id} "
            f"for validation in Playground."
        )
    )

    agent_version = new_agent.version

    update_trackor_with_agent_info(
        trackor_id=trackor_id,
        agent_version=agent_version
    )

    logger.info(
        "Created new agent version for %s (trackor=%s) "
        "model=%s with Code Interpreter file_id=%s",
        agent_name, trackor_id, model_deployment_name, file_id
    )


# ------------------------------------------------------------------
# Main: iterate all Report_Repository trackors found via endpoint
# ------------------------------------------------------------------
def main():
    rr_trackors = list_report_repository_trackors()
    logger.info("Found %d Report_Repository trackors to process", len(rr_trackors))

    # Optional fallback defaults (ONLY if RR fields are missing)
    fallback_project_endpoint = get_api_key("PROJECT_ENDPOINT")
    fallback_agent_name = get_section("agent").get("existing_agent_name")

    for rec in rr_trackors:
        trackor_id = rec.get("TRACKOR_ID")
        rr_agent_name = rec.get("RR_AZURE_AGENT_NAME")
        rr_project_endpoint = rec.get("RR_AZURE_PROJECT_ENDPOINT")

        if not trackor_id:
            logger.warning("Skipping record with no TRACKOR_ID: %s", rec)
            continue

        # (2) Use values from response; fall back only if missing
        agent_name = rr_agent_name or fallback_agent_name
        project_endpoint = rr_project_endpoint or fallback_project_endpoint

        if not agent_name or not project_endpoint:
            logger.warning(
                "Skipping TRACKOR_ID=%s because agent_name or project_endpoint is missing "
                "(RR_AZURE_AGENT_NAME=%s, RR_AZURE_PROJECT_ENDPOINT=%s, fallback_agent=%s, fallback_endpoint=%s)",
                trackor_id, rr_agent_name, rr_project_endpoint, fallback_agent_name, fallback_project_endpoint
            )
            continue

        logger.info("=== Processing TRACKOR_ID=%s agent=%s ===", trackor_id, agent_name)

        # Download & extract per-trackor
        raw_bytes = download_onevizion_efile(trackor_id=trackor_id)
        csv_bytes, csv_name = extract_csv(raw_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = f"{tmp}/{csv_name}"
            with open(csv_path, "wb") as f:
                f.write(csv_bytes)

            # 1) Upload file to Foundry (per-trackor endpoint)
            file_id = upload_file_only(project_endpoint=project_endpoint, csv_path=csv_path)

        # 2) Write file_id back to OneVizion (per-trackor)
        update_trackor_with_file_id(trackor_id=trackor_id, file_id=file_id)

        # 3) Attach file to existing agent by creating a new version with CI + file_id (per-trackor)
        attach_file_to_existing_agent_code_interpreter(
            trackor_id=trackor_id,
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            file_id=file_id
        )

        logger.info("✅ Upload complete for TRACKOR_ID=%s (File ID: %s)", trackor_id, file_id)

    print("✅ Done processing all matching Report_Repository trackors.")


if __name__ == "__main__":
    main()
