import json
import io
import zipfile
import tempfile
import requests
import logging


# ====== For use in the app server environment
# subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', 'python_dependencies.ini'])

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
def get_api_key(api_name):
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


def get_section(section_name):
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            return data.get(section_name, {})
    except Exception:
        logger.exception("Failed reading settings.json")
        return {}


# ------------------------------------------------------------------
# OneVizion: download E-File using Bearer token
# ------------------------------------------------------------------
def download_onevizion_efile():
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")

    ov_cfg = get_section("onevizion")
    trackor_id = ov_cfg["trackor_id"]
    field_name = ov_cfg["source_efile_field_name"]

    if not base_url or not bearer_token:
        raise Exception("Missing OneVizion base URL or bearer token")

    url = f"{base_url.rstrip('/')}/api/v3/trackor/{trackor_id}/file/{field_name}"

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/octet-stream"
    }

    logger.info("Downloading OneVizion E-File from %s", url)
    response = requests.get(url, headers=headers, timeout=120)
    response.raise_for_status()

    return response.content


# ------------------------------------------------------------------
# Handle Single E-File or Multi E-File (ZIP)
# ------------------------------------------------------------------
def extract_csv(file_bytes):
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

    # Single-file E-File
    return file_bytes, "onevizion_data.csv"


# ------------------------------------------------------------------
# Azure Foundry: upload file only (no agent)
# ------------------------------------------------------------------
def upload_file_only(csv_path):
    project_endpoint = get_api_key("PROJECT_ENDPOINT")
    if not project_endpoint:
        raise Exception("PROJECT_ENDPOINT not configured")

    credential = ClientSecretCredential(
        tenant_id=get_api_key("TENANT_ID"),
        client_id=get_api_key("CLIENT_ID"),
        client_secret=get_api_key("CLIENT_SECRET")
    )

    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential
    )

    openai_client = project_client.get_openai_client()

    with open(csv_path, "rb") as f:
        uploaded = openai_client.files.create(
            file=f,
            purpose="assistants"
        )

    logger.info("File uploaded successfully: file_id=%s", uploaded.id)
    return uploaded.id
# ------------------------------------------------------------------
# OneVizion: Load Azure File ID to RR Text Field
# ------------------------------------------------------------------
def update_trackor_with_file_id(file_id: str):
    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")

    ov_cfg = get_section("onevizion")
    trackor_id = ov_cfg["trackor_id"]
    field_name = ov_cfg["target_file_id_field_name"]

    url = f"{base_url.rstrip('/')}/api/v3/trackors/{trackor_id}"

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
    }

    payload = {
        field_name: file_id
    }

    logger.info(
        "Updating OneVizion Trackor %s field %s with file_id %s",
        trackor_id, field_name, file_id
    )

    response = requests.put(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()


# ------------------------------------------------------------------
# NEW: Attach the uploaded file to an EXISTING agent (create new version)
# ------------------------------------------------------------------
def attach_file_to_existing_agent_code_interpreter(file_id: str):
    """
    Creates a NEW VERSION of an existing agent name and configures Code Interpreter
    with the uploaded file_id so you can validate the file in Agents Playground.

    This follows the Foundry Code Interpreter pattern: attach files via file_ids in
    the Code Interpreter tool configuration. [1](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/code-interpreter)
    """
    project_endpoint = get_api_key("PROJECT_ENDPOINT")
    model_name = get_api_key("MODEL_DEPLOYMENT_NAME")

    agent_cfg = get_section("agent")
    agent_name = agent_cfg["existing_agent_name"]
    instructions = agent_cfg.get(
        "instructions_for_new_version",
        "Use Code Interpreter to analyze the attached CSV."
    )

    if not project_endpoint or not model_name:
        raise Exception("Missing PROJECT_ENDPOINT or MODEL_DEPLOYMENT_NAME")

    credential = ClientSecretCredential(
        tenant_id=get_api_key("TENANT_ID"),
        client_id=get_api_key("CLIENT_ID"),
        client_secret=get_api_key("CLIENT_SECRET")
    )

    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential
    )

    code_interpreter = CodeInterpreterTool(
        container=AutoCodeInterpreterToolParam(file_ids=[file_id])
    )

    # Create a new version under the SAME agent name
    new_agent = project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=model_name,
            instructions=instructions,
            tools=[code_interpreter]
        ),
        description=f"Auto version created to attach file {file_id} for validation in Playground."
    )

    agent_version = new_agent.version

    update_trackor_with_agent_info(
        agent_version=agent_version
    )

    logger.info("Created new agent version for %s with Code Interpreter file_id=%s",
                agent_name, file_id)

# ------------------------------------------------------------------
# NEW: Save Agent ID and Version Number to RR Fields
# ------------------------------------------------------------------
def update_trackor_with_agent_info(agent_version: int):
    """
    Writes the Agent ID and Agent Version created in Azure Foundry
    back to OneVizion fields:
      - RR_AGENT_ID (string)
      - RR_AGENT_VERSION (number)
    """

    base_url = get_api_key("OV_BASE_URL")
    bearer_token = get_api_key("ONEVIZION_BEARER_TOKEN")

    ov_cfg = get_section("onevizion")

    trackor_type_id = ov_cfg.get("trackor_type_id")
    trackor_id = ov_cfg.get("trackor_id")

    if not all([base_url, bearer_token, trackor_type_id, trackor_id]):
        raise Exception("OneVizion configuration incomplete")

    url = f"{base_url}/api/v3/trackor_types/{trackor_type_id}/trackors/{trackor_id}"

    payload = {
        "fields": [
            {
                "fieldName": "RR_AGENT_VERSION",
                "value": agent_version
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
    }

    response = requests.put(url, headers=headers, json=payload)

    if not response.ok:
        logger.error("Failed updating Trackor with agent info: %s", response.text)
        response.raise_for_status()

    logger.info(
        "Updated OV Trackor with Version=%s",
        agent_id,
        agent_version
    )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    raw_bytes = download_onevizion_efile()
    csv_bytes, csv_name = extract_csv(raw_bytes)

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = f"{tmp}/{csv_name}"
        with open(csv_path, "wb") as f:
            f.write(csv_bytes)

        # 1) Upload file to Foundry
        file_id = upload_file_only(csv_path)

    # 2) Write file_id back to OneVizion (different field, same trackor)
    update_trackor_with_file_id(file_id)

    # 3) NEW: Attach file to existing agent by creating a new version with CI + file_id
    attach_file_to_existing_agent_code_interpreter(file_id)

    print("✅ Upload complete")
    print(f"File ID: {file_id}")


if __name__ == "__main__":
    main()
