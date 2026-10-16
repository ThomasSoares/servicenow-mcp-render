import os
import secrets
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn


# =========================================================
# 1. LOAD SERVICENOW SETTINGS
# =========================================================

load_dotenv()

SN_INSTANCE = os.getenv("SN_INSTANCE", "").rstrip("/")
SN_USERNAME = os.getenv("SN_USERNAME", "")
SN_PASSWORD = os.getenv("SN_PASSWORD", "")

if not SN_INSTANCE or not SN_USERNAME or not SN_PASSWORD:
    raise RuntimeError(
        "ServiceNow configuration is missing. "
        "Set SN_INSTANCE, SN_USERNAME and SN_PASSWORD in Render Environment (or your local .env)."
    )


# =========================================================
# 2. CREATE MCP SERVER
# =========================================================

# This deployment uses the official MCP Python SDK 1.26.0 (requirements.txt).
instance_url = urlsplit(SN_INSTANCE)
if (instance_url.scheme != "https" or not instance_url.hostname
        or instance_url.username or instance_url.password
        or instance_url.path or instance_url.query or instance_url.fragment):
    raise RuntimeError("SN_INSTANCE must be an HTTPS base URL, e.g. https://dev123.service-now.com")

MCP_API_TOKEN = os.getenv("MCP_API_TOKEN", "")
if len(MCP_API_TOKEN) < 32 or not MCP_API_TOKEN.isascii() or any(c.isspace() for c in MCP_API_TOKEN):
    raise RuntimeError("Set MCP_API_TOKEN to a random ASCII token of at least 32 characters, without spaces.")

PORT = int(os.getenv("PORT", "8000"))
allowed_hosts = ["localhost:*", "127.0.0.1:*"]
allowed_origins = ["http://localhost:*", "http://127.0.0.1:*"]
render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if render_hostname:
    allowed_hosts.extend([render_hostname, f"{render_hostname}:443"])
    allowed_origins.append(f"https://{render_hostname}")
# Only needed if a browser-based client sends this Origin header.
allowed_origins.append("https://nblive2026.netbrain.com")

mcp = FastMCP(
    "My ServiceNow MCP Server",
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ),
)


# =========================================================
# 3. SERVICENOW REST API HELPER
# =========================================================

def servicenow_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:

    url = f"{SN_INSTANCE}{path}"

    try:
        with httpx.Client(
            auth=(SN_USERNAME, SN_PASSWORD),
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        ) as client:

            response = client.request(
                method=method,
                url=url,
                params=params,
                json=data,
            )

            response.raise_for_status()

            if not response.content:
                return {}

            return response.json()

    except httpx.HTTPStatusError as error:
        raise RuntimeError(
            f"ServiceNow returned HTTP "
            f"{error.response.status_code}: "
            f"{error.response.text}"
        ) from error

    except httpx.RequestError as error:
        raise RuntimeError(
            f"Could not connect to ServiceNow: {str(error)}"
        ) from error


# =========================================================
# 4. GENERAL HELPERS
# =========================================================

def get_reference_value(value: Any) -> str:

    if isinstance(value, dict):
        return str(value.get("value", ""))

    if value is None:
        return ""

    return str(value)


def clean_payload(
    data: dict[str, Any],
) -> dict[str, Any]:

    return {
        key: value
        for key, value in data.items()
        if value is not None
    }


# Keyword search helpers and incident search tool

def sanitize_query_text(text: str) -> str:
    """
    Removes characters that could accidentally alter a
    ServiceNow encoded query.
    """

    return (
        text
        .strip()
        .replace("^", " ")
        .replace("=", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def parse_keywords(keywords: str) -> list[str]:
    """
    Multiple keywords can be separated by commas.

    Examples:

    router down
        -> ["router down"]

    BGP, neighbor, flap
        -> ["BGP", "neighbor", "flap"]
    """

    parts = [
        sanitize_query_text(item)
        for item in keywords.split(",")
        if item.strip()
    ]

    # Remove duplicates while keeping original order
    unique_parts = []

    for item in parts:
        if item and item.lower() not in [
            existing.lower()
            for existing in unique_parts
        ]:
            unique_parts.append(item)

    return unique_parts


def build_text_search_query(
    base_conditions: list[str],
    keywords: list[str],
) -> str:
    """
    Builds a ServiceNow encoded query that searches
    short_description OR description.

    Multiple keywords are treated as OR searches.

    Example:

    keywords:
        BGP, neighbor

    Means approximately:

        short_description contains BGP
        OR description contains BGP
        OR short_description contains neighbor
        OR description contains neighbor
    """

    if not keywords:

        query = "^".join(base_conditions)

        if query:
            query += "^"

        return query + "ORDERBYDESCsys_updated_on"

    branches = []

    for keyword in keywords:

        short_description_branch = (
            base_conditions
            + [
                f"short_descriptionLIKE{keyword}"
            ]
        )

        description_branch = (
            base_conditions
            + [
                f"descriptionLIKE{keyword}"
            ]
        )

        branches.append(
            "^".join(short_description_branch)
        )

        branches.append(
            "^".join(description_branch)
        )

    query = "^NQ".join(branches)

    query += "^ORDERBYDESCsys_updated_on"

    return query



@mcp.tool()
def search_incidents(
    keywords: str,
    days_back: int = 30,
    limit: int = 20,
) -> dict[str, Any]:
    """
    Searches ServiceNow incidents by text found in either:

    - short_description
    - description

    One word or phrase may be supplied:

        router down

    Multiple search terms may be separated by commas:

        BGP, neighbor, flap

    Multiple terms are treated as OR searches.

    days_back filters by creation date. Results are sorted by latest update.

    limit controls the maximum number of records returned.
    """

    if days_back < 0:
        raise ValueError(
            "days_back cannot be negative."
        )

    if limit < 1:
        raise ValueError(
            "limit must be at least 1."
        )

    limit = min(limit, 100)

    parsed_keywords = parse_keywords(
        keywords
    )

    if not parsed_keywords:
        raise ValueError(
            "At least one search keyword is required."
        )

    start_time = (
        datetime.now(timezone.utc)
        - timedelta(days=days_back)
    ).strftime("%Y-%m-%d %H:%M:%S")

    base_conditions = [
        f"sys_created_on>={start_time}"
    ]

    query = build_text_search_query(
        base_conditions,
        parsed_keywords,
    )

    response = servicenow_request(
        "GET",
        "/api/now/table/incident",
        params={
            "sysparm_query":
                query,
            "sysparm_limit":
                str(limit),
            "sysparm_fields": (
                "sys_id,"
                "number,"
                "short_description,"
                "description,"
                "state,"
                "priority,"
                "impact,"
                "urgency,"
                "caller_id,"
                "assigned_to,"
                "assignment_group,"
                "opened_at,"
                "sys_created_on,"
                "sys_updated_on"
            ),
            "sysparm_display_value":
                "true",
        },
    )

    incidents = response.get(
        "result",
        [],
    )

    return {
        "keywords":
            parsed_keywords,
        "days_back":
            days_back,
        "count":
            len(incidents),
        "incidents":
            incidents,
    }




# =========================================================
# 5. SERVICENOW CHOICE HELPERS
# =========================================================
#
# Fields such as:
#
# incident.close_code
# change_request.close_code
# change_request.state
#
# often have an internal value different from the label shown
# in the GUI.
#
# These functions allow us to use the human-readable label.
#

def get_choices(
    table: str,
    element: str,
) -> list[dict[str, Any]]:

    response = servicenow_request(
        "GET",
        "/api/now/table/sys_choice",
        params={
            "sysparm_query": (
                f"name={table}"
                f"^element={element}"
                "^inactive=false"
                "^ORDERBYsequence"
            ),
            "sysparm_limit": "100",
            "sysparm_fields":
                "label,value,sequence",
        },
    )

    return response.get("result", [])


def resolve_choice(
    table: str,
    element: str,
    requested_value: str,
) -> dict[str, str]:

    choices = get_choices(
        table,
        element,
    )

    requested = requested_value.strip().lower()

    for choice in choices:

        label = str(
            choice.get("label", "")
        ).strip()

        value = str(
            choice.get("value", "")
        ).strip()

        if (
            requested == label.lower()
            or requested == value.lower()
        ):
            return {
                "label": label,
                "value": value,
            }

    valid_labels = [
        choice.get("label")
        for choice in choices
        if choice.get("label")
    ]

    raise ValueError(
        f"'{requested_value}' is not valid for "
        f"{table}.{element}. "
        f"Valid options are: {valid_labels}"
    )


# =========================================================
# 6. USER LOOKUP
# =========================================================
#
# Allows caller/requested_by/assigned_to to be entered as:
#
# admin
# Fred Luddy
# fred@example.com
# sys_id
#

def resolve_user(
    user: str,
) -> dict[str, Any]:

    safe_user = user.strip().replace("^", "")

    response = servicenow_request(
        "GET",
        "/api/now/table/sys_user",
        params={
            "sysparm_query": (
                f"sys_id={safe_user}"
                f"^ORuser_name={safe_user}"
                f"^ORemail={safe_user}"
                f"^ORname={safe_user}"
            ),
            "sysparm_limit": "10",
            "sysparm_fields":
                "sys_id,user_name,name,email,active",
            "sysparm_display_value": "true",
        },
    )

    users = response.get("result", [])

    if not users:
        raise ValueError(
            f"No ServiceNow user could be found "
            f"for '{user}'."
        )

    if len(users) > 1:

        matches = []

        for found_user in users:
            matches.append({
                "name":
                    found_user.get("name"),
                "user_name":
                    found_user.get("user_name"),
                "email":
                    found_user.get("email"),
                "sys_id":
                    found_user.get("sys_id"),
            })

        raise ValueError(
            f"More than one user matched '{user}'. "
            f"Use the username, email or sys_id. "
            f"Matches: {matches}"
        )

    return users[0]


# =========================================================
# 7. GROUP LOOKUP
# =========================================================

def resolve_group(
    group: str,
) -> dict[str, Any]:

    safe_group = group.strip().replace("^", "")

    response = servicenow_request(
        "GET",
        "/api/now/table/sys_user_group",
        params={
            "sysparm_query": (
                f"sys_id={safe_group}"
                f"^ORname={safe_group}"
            ),
            "sysparm_limit": "10",
            "sysparm_fields":
                "sys_id,name,active",
            "sysparm_display_value": "true",
        },
    )

    groups = response.get("result", [])

    if not groups:
        raise ValueError(
            f"No ServiceNow assignment group could "
            f"be found for '{group}'."
        )

    if len(groups) > 1:

        matches = [
            {
                "name": item.get("name"),
                "sys_id": item.get("sys_id"),
            }
            for item in groups
        ]

        raise ValueError(
            f"More than one group matched '{group}'. "
            f"Use the exact name or sys_id. "
            f"Matches: {matches}"
        )

    return groups[0]


# =========================================================
# 8. CONFIGURATION ITEM LOOKUP
# =========================================================
#
# Allows you to use a CI name instead of manually finding
# the sys_id.
#

def resolve_ci(
    configuration_item: str,
) -> dict[str, Any]:

    safe_ci = (
        configuration_item
        .strip()
        .replace("^", "")
    )

    response = servicenow_request(
        "GET",
        "/api/now/table/cmdb_ci",
        params={
            "sysparm_query": (
                f"sys_id={safe_ci}"
                f"^ORname={safe_ci}"
                f"^ORasset_tag={safe_ci}"
                f"^ORserial_number={safe_ci}"
            ),
            "sysparm_limit": "10",
            "sysparm_fields": (
                "sys_id,"
                "name,"
                "sys_class_name,"
                "asset_tag,"
                "serial_number"
            ),
            "sysparm_display_value": "true",
        },
    )

    cis = response.get("result", [])

    if not cis:
        raise ValueError(
            f"No Configuration Item could be found "
            f"for '{configuration_item}'."
        )

    if len(cis) > 1:

        matches = [
            {
                "name":
                    ci.get("name"),
                "class":
                    ci.get("sys_class_name"),
                "serial_number":
                    ci.get("serial_number"),
                "sys_id":
                    ci.get("sys_id"),
            }
            for ci in cis
        ]

        raise ValueError(
            f"More than one Configuration Item matched "
            f"'{configuration_item}'. "
            f"Use the exact CI name or sys_id. "
            f"Matches: {matches}"
        )

    return cis[0]


# =========================================================
# 9. FIND INCIDENT
# =========================================================

def find_incident(
    incident_number: str,
    display_values: bool = False,
) -> dict[str, Any] | None:

    response = servicenow_request(
        "GET",
        "/api/now/table/incident",
        params={
            "sysparm_query":
                f"number={incident_number.upper()}",
            "sysparm_limit": "1",
            "sysparm_fields": (
                "sys_id,"
                "number,"
                "short_description,"
                "description,"
                "state,"
                "priority,"
                "impact,"
                "urgency,"
                "caller_id,"
                "assigned_to,"
                "assignment_group,"
                "opened_at,"
                "sys_updated_on,"
                "close_code,"
                "close_notes"
            ),
            "sysparm_display_value":
                "true" if display_values else "false",
        },
    )

    incidents = response.get("result", [])

    if not incidents:
        return None

    return incidents[0]


# =========================================================
# 10. FIND CHANGE REQUEST
# =========================================================

def find_change(
    change_number: str,
    display_values: bool = False,
) -> dict[str, Any] | None:

    response = servicenow_request(
        "GET",
        "/api/now/table/change_request",
        params={
            "sysparm_query":
                f"number={change_number.upper()}",
            "sysparm_limit": "1",
            "sysparm_fields": (
                "sys_id,"
                "number,"
                "short_description,"
                "description,"
                "state,"
                "type,"
                "risk,"
                "impact,"
                "priority,"
                "cmdb_ci,"
                "requested_by,"
                "assigned_to,"
                "assignment_group,"
                "start_date,"
                "end_date,"
                "implementation_plan,"
                "backout_plan,"
                "test_plan,"
                "close_code,"
                "close_notes,"
                "active,"
                "opened_at,"
                "sys_created_on,"
                "sys_updated_on"
            ),
            "sysparm_display_value":
                "true" if display_values else "false",
        },
    )

    changes = response.get("result", [])

    if not changes:
        return None

    return changes[0]


# =========================================================
# MCP TOOL 1
# TEST CONNECTION
# =========================================================

@mcp.tool()
def test_servicenow_connection() -> dict[str, Any]:
    """
    Tests whether the MCP server can connect to ServiceNow.
    """

    response = servicenow_request(
        "GET",
        "/api/now/table/incident",
        params={
            "sysparm_limit": "1",
            "sysparm_fields":
                "number,short_description",
        },
    )

    incidents = response.get("result", [])

    return {
        "connected": True,
        "records_returned": len(incidents),
        "sample": incidents,
    }


# =========================================================
# MCP TOOL 2
# GET INCIDENT
# =========================================================

@mcp.tool()
def get_incident(
    incident_number: str,
) -> dict[str, Any]:
    """
    Retrieves a ServiceNow incident using its incident number.
    """

    incident = find_incident(
        incident_number,
        display_values=True,
    )

    if not incident:
        return {
            "found": False,
            "message":
                f"{incident_number.upper()} was not found."
        }

    return {
        "found": True,
        "incident": incident,
    }


# =========================================================
# MCP TOOL 3
# CREATE INCIDENT
# =========================================================

@mcp.tool()
def create_incident(
    short_description: str,
    caller: str,
    description: str = "",
    urgency: int = 3,
    impact: int = 3,
) -> dict[str, Any]:
    """
    Creates a new ServiceNow incident.

    caller may be username, name, email or sys_id.

    urgency:
        1 = High
        2 = Medium
        3 = Low

    impact:
        1 = High
        2 = Medium
        3 = Low
    """

    if urgency not in (1, 2, 3):
        raise ValueError(
            "Urgency must be 1, 2, or 3."
        )

    if impact not in (1, 2, 3):
        raise ValueError(
            "Impact must be 1, 2, or 3."
        )

    caller_user = resolve_user(caller)

    response = servicenow_request(
        "POST",
        "/api/now/table/incident",
        data={
            "short_description":
                short_description,
            "description":
                description,
            "caller_id":
                caller_user["sys_id"],
            "urgency":
                str(urgency),
            "impact":
                str(impact),
        },
    )

    incident = response.get("result", {})

    return {
        "created": True,
        "number":
            incident.get("number"),
        "sys_id":
            incident.get("sys_id"),
        "short_description":
            incident.get("short_description"),
        "caller": {
            "name":
                caller_user.get("name"),
            "user_name":
                caller_user.get("user_name"),
        },
        "state":
            incident.get("state"),
        "priority":
            incident.get("priority"),
    }


# =========================================================
# MCP TOOL 4
# UPDATE INCIDENT
# =========================================================

@mcp.tool()
def update_incident(
    incident_number: str,
    short_description: str | None = None,
    description: str | None = None,
    work_notes: str | None = None,
    caller: str | None = None,
    urgency: int | None = None,
    impact: int | None = None,
) -> dict[str, Any]:
    """
    Updates fields on an existing ServiceNow incident.
    """

    incident = find_incident(
        incident_number,
        display_values=False,
    )

    if not incident:
        return {
            "updated": False,
            "message":
                f"{incident_number.upper()} was not found."
        }

    data: dict[str, Any] = {}

    if short_description is not None:
        data["short_description"] = short_description

    if description is not None:
        data["description"] = description

    if work_notes is not None:
        data["work_notes"] = work_notes

    if caller is not None:
        data["caller_id"] = (
            resolve_user(caller)["sys_id"]
        )

    if urgency is not None:

        if urgency not in (1, 2, 3):
            raise ValueError(
                "Urgency must be 1, 2, or 3."
            )

        data["urgency"] = str(urgency)

    if impact is not None:

        if impact not in (1, 2, 3):
            raise ValueError(
                "Impact must be 1, 2, or 3."
            )

        data["impact"] = str(impact)

    if not data:
        return {
            "updated": False,
            "message":
                "No fields were provided to update."
        }

    response = servicenow_request(
        "PATCH",
        f"/api/now/table/incident/"
        f"{incident['sys_id']}",
        data=data,
    )

    result = response.get("result", {})

    return {
        "updated": True,
        "number":
            result.get(
                "number",
                incident_number.upper()
            ),
        "updated_fields":
            list(data.keys()),
    }


# =========================================================
# MCP TOOL 5
# CLOSE INCIDENT
# =========================================================

@mcp.tool()
def close_incident(
    incident_number: str,
    resolution_notes: str,
    close_code: str,
    caller: str | None = None,
) -> dict[str, Any]:
    """
    Closes a ServiceNow incident.

    The resolution code may be supplied using the label shown
    in ServiceNow, such as 'Resolved by caller'.
    """

    incident = find_incident(
        incident_number,
        display_values=False,
    )

    if not incident:
        return {
            "closed": False,
            "message":
                f"{incident_number.upper()} was not found."
        }

    if str(incident.get("state")) == "7":
        return {
            "closed": False,
            "message":
                f"{incident_number.upper()} "
                f"is already Closed."
        }

    resolved_code = resolve_choice(
        "incident",
        "close_code",
        close_code,
    )

    close_data: dict[str, Any] = {
        "state": "7",
        "close_notes":
            resolution_notes,
        "close_code":
            resolved_code["value"],
    }

    existing_caller = get_reference_value(
        incident.get("caller_id")
    )

    if caller is not None:
        close_data["caller_id"] = (
            resolve_user(caller)["sys_id"]
        )

    elif not existing_caller:
        return {
            "closed": False,
            "caller_required": True,
            "message": (
                f"{incident_number.upper()} does not "
                f"have a Caller. Supply the caller "
                f"parameter before closing it."
            ),
        }

    response = servicenow_request(
        "PATCH",
        f"/api/now/table/incident/"
        f"{incident['sys_id']}",
        data=close_data,
    )

    result = response.get("result", {})

    return {
        "closed": True,
        "number":
            result.get(
                "number",
                incident_number.upper()
            ),
        "state":
            result.get("state"),
        "resolution_code":
            resolved_code["label"],
        "resolution_notes":
            resolution_notes,
    }


# =========================================================
# MCP TOOL 6
# GET CHANGE REQUEST
# =========================================================

@mcp.tool()
def get_change(
    change_number: str,
) -> dict[str, Any]:
    """
    Retrieves a ServiceNow Change Request using its CHG number.

    Example:
    CHG0030001
    """

    change = find_change(
        change_number,
        display_values=True,
    )

    if not change:
        return {
            "found": False,
            "message":
                f"{change_number.upper()} was not found."
        }

    return {
        "found": True,
        "change": change,
    }


# =========================================================
# MCP TOOL 7
# CREATE CHANGE REQUEST
# =========================================================

@mcp.tool()
def create_change(
    short_description: str,
    description: str = "",
    change_type: str = "normal",
    configuration_item: str | None = None,
    requested_by: str | None = None,
    assignment_group: str | None = None,
    assigned_to: str | None = None,
    implementation_plan: str | None = None,
    backout_plan: str | None = None,
    test_plan: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Creates a ServiceNow Change Request.

    configuration_item may be a CI name or sys_id.

    requested_by and assigned_to may be username, name,
    email or sys_id.

    assignment_group may be group name or sys_id.

    Typical change_type values include:
    normal
    standard
    emergency
    """

    data: dict[str, Any] = {
        "short_description":
            short_description,
        "description":
            description,
        "type":
            change_type,
    }

    if configuration_item:
        ci = resolve_ci(
            configuration_item
        )

        data["cmdb_ci"] = (
            ci["sys_id"]
        )

    if requested_by:
        data["requested_by"] = (
            resolve_user(
                requested_by
            )["sys_id"]
        )

    if assignment_group:
        data["assignment_group"] = (
            resolve_group(
                assignment_group
            )["sys_id"]
        )

    if assigned_to:
        data["assigned_to"] = (
            resolve_user(
                assigned_to
            )["sys_id"]
        )

    if implementation_plan is not None:
        data["implementation_plan"] = (
            implementation_plan
        )

    if backout_plan is not None:
        data["backout_plan"] = (
            backout_plan
        )

    if test_plan is not None:
        data["test_plan"] = (
            test_plan
        )

    if start_date is not None:
        data["start_date"] = start_date

    if end_date is not None:
        data["end_date"] = end_date

    response = servicenow_request(
        "POST",
        "/api/now/table/change_request",
        data=data,
    )

    change = response.get("result", {})

    return {
        "created": True,
        "number":
            change.get("number"),
        "sys_id":
            change.get("sys_id"),
        "short_description":
            change.get("short_description"),
        "state":
            change.get("state"),
        "type":
            change.get("type"),
        "configuration_item":
            change.get("cmdb_ci"),
    }


# =========================================================
# MCP TOOL 8
# UPDATE CHANGE REQUEST
# =========================================================

@mcp.tool()
def update_change(
    change_number: str,
    short_description: str | None = None,
    description: str | None = None,
    work_notes: str | None = None,
    configuration_item: str | None = None,
    requested_by: str | None = None,
    assignment_group: str | None = None,
    assigned_to: str | None = None,
    implementation_plan: str | None = None,
    backout_plan: str | None = None,
    test_plan: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """
    Updates supported fields on a ServiceNow Change Request.

    state may be supplied using the ServiceNow label,
    for example 'Assess', 'Authorize', 'Scheduled',
    'Implement', 'Review', or 'Closed', depending on
    the choices available in your PDI.
    """

    change = find_change(
        change_number,
        display_values=False,
    )

    if not change:
        return {
            "updated": False,
            "message":
                f"{change_number.upper()} was not found."
        }

    data: dict[str, Any] = {}

    if short_description is not None:
        data["short_description"] = (
            short_description
        )

    if description is not None:
        data["description"] = description

    if work_notes is not None:
        data["work_notes"] = work_notes

    if configuration_item is not None:
        data["cmdb_ci"] = (
            resolve_ci(
                configuration_item
            )["sys_id"]
        )

    if requested_by is not None:
        data["requested_by"] = (
            resolve_user(
                requested_by
            )["sys_id"]
        )

    if assignment_group is not None:
        data["assignment_group"] = (
            resolve_group(
                assignment_group
            )["sys_id"]
        )

    if assigned_to is not None:
        data["assigned_to"] = (
            resolve_user(
                assigned_to
            )["sys_id"]
        )

    if implementation_plan is not None:
        data["implementation_plan"] = (
            implementation_plan
        )

    if backout_plan is not None:
        data["backout_plan"] = (
            backout_plan
        )

    if test_plan is not None:
        data["test_plan"] = (
            test_plan
        )

    if start_date is not None:
        data["start_date"] = start_date

    if end_date is not None:
        data["end_date"] = end_date

    if state is not None:

        resolved_state = resolve_choice(
            "change_request",
            "state",
            state,
        )

        data["state"] = (
            resolved_state["value"]
        )

    if not data:
        return {
            "updated": False,
            "message":
                "No fields were provided to update."
        }

    response = servicenow_request(
        "PATCH",
        f"/api/now/table/change_request/"
        f"{change['sys_id']}",
        data=data,
    )

    result = response.get("result", {})

    return {
        "updated": True,
        "number":
            result.get(
                "number",
                change_number.upper()
            ),
        "updated_fields":
            list(data.keys()),
        "state":
            result.get("state"),
    }


# =========================================================
# MCP TOOL 9
# SEARCH CHANGE REQUESTS
# =========================================================

@mcp.tool()
def search_changes(
    configuration_item: str | None = None,
    days_back: int = 7,
    keyword: str | None = None,
    limit: int = 20,
    keywords: str | None = None,
) -> dict[str, Any]:
    """Search changes by text in short_description or description.

    configuration_item is optional (CI name or sys_id).
    keywords accepts a phrase such as 'router down', or comma-separated
    terms such as 'BGP, neighbor, flap'. Any term may match either field.
    keyword is a legacy alias; supply only one of keyword and keywords.
    days_back filters by creation date (default 7 days); results are sorted
    by most recently updated. limit is capped at 100.
    Supply keywords, a configuration_item, or both.
    """
    if days_back < 0:
        raise ValueError("days_back cannot be negative.")
    if limit < 1:
        raise ValueError("limit must be at least 1.")
    limit = min(limit, 100)
    if keyword is not None and keywords is not None:
        raise ValueError("Supply only keywords or keyword, not both.")
    text = keywords if keywords is not None else keyword
    parsed_keywords = parse_keywords(text or "")
    if text is not None and not parsed_keywords:
        raise ValueError("At least one search keyword is required when supplying keywords.")
    configuration_item = (configuration_item or "").strip()
    if not configuration_item and not parsed_keywords:
        raise ValueError("Supply keywords or a configuration_item.")
    start_time = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")
    base_conditions = [f"sys_created_on>={start_time}"]
    ci = None
    if configuration_item:
        ci = resolve_ci(configuration_item)
        base_conditions.append(f"cmdb_ci={ci['sys_id']}")
    query = build_text_search_query(base_conditions, parsed_keywords)
    response = servicenow_request(
        "GET", "/api/now/table/change_request",
        params={
            "sysparm_query": query,
            "sysparm_limit": str(limit),
            "sysparm_fields": (
                "sys_id,number,short_description,description,state,type,risk,impact,"
                "priority,cmdb_ci,requested_by,assigned_to,assignment_group,"
                "start_date,end_date,sys_created_on,sys_updated_on"
            ),
            "sysparm_display_value": "true",
        },
    )
    changes = response.get("result", [])
    return {
        "configuration_item": {"name": ci.get("name"), "sys_id": ci["sys_id"]} if ci else None,
        "keyword": text,
        "keywords": parsed_keywords,
        "days_back": days_back,
        "count": len(changes),
        "changes": changes,
    }


# =========================================================
# MCP TOOL 10
# CLOSE CHANGE REQUEST
# =========================================================

@mcp.tool()
def close_change(
    change_number: str,
    close_code: str,
    close_notes: str,
) -> dict[str, Any]:
    """
    Closes a ServiceNow Change Request.

    Before closing the Change Request, this tool checks for
    active Change Tasks.

    If any active Change Tasks remain, the Change Request
    WILL NOT be closed.

    close_code may be supplied using the label shown in
    ServiceNow.
    """

    change = find_change(
        change_number,
        display_values=False,
    )

    if not change:
        return {
            "closed": False,
            "message":
                f"{change_number.upper()} was not found."
        }

    change_sys_id = (
        change["sys_id"]
    )

    # -----------------------------------------------------
    # Check for active Change Tasks
    # -----------------------------------------------------

    task_response = servicenow_request(
        "GET",
        "/api/now/table/change_task",
        params={
            "sysparm_query": (
                f"change_request={change_sys_id}"
                "^active=true"
            ),
            "sysparm_limit": "100",
            "sysparm_fields": (
                "sys_id,"
                "number,"
                "short_description,"
                "state,"
                "active,"
                "assigned_to,"
                "assignment_group"
            ),
            "sysparm_display_value":
                "true",
        },
    )

    active_tasks = task_response.get(
        "result",
        [],
    )

    if active_tasks:

        return {
            "closed": False,
            "pending_change_tasks": True,
            "message": (
                f"{change_number.upper()} has "
                f"{len(active_tasks)} active Change Task(s). "
                f"The Change Request was not closed."
            ),
            "active_change_tasks":
                active_tasks,
        }

    # -----------------------------------------------------
    # Resolve close code
    # -----------------------------------------------------

    resolved_close_code = resolve_choice(
        "change_request",
        "close_code",
        close_code,
    )

    # -----------------------------------------------------
    # Resolve Closed state dynamically
    # -----------------------------------------------------

    resolved_closed_state = resolve_choice(
        "change_request",
        "state",
        "Closed",
    )

    # -----------------------------------------------------
    # Close Change Request
    # -----------------------------------------------------

    response = servicenow_request(
        "PATCH",
        f"/api/now/table/change_request/"
        f"{change_sys_id}",
        data={
            "state":
                resolved_closed_state["value"],
            "close_code":
                resolved_close_code["value"],
            "close_notes":
                close_notes,
        },
    )

    result = response.get(
        "result",
        {},
    )

    return {
        "closed": True,
        "number":
            result.get(
                "number",
                change_number.upper()
            ),
        "state": {
            "label":
                resolved_closed_state["label"],
            "value":
                resolved_closed_state["value"],
        },
        "close_code": {
            "label":
                resolved_close_code["label"],
            "value":
                resolved_close_code["value"],
        },
        "close_notes":
            close_notes,
    }


# =========================================================
# START MCP SERVER
# =========================================================

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    # Liveness only: does not contact ServiceNow or expose configuration.
    return JSONResponse({"status": "ok"})


class BearerTokenMiddleware:
    """Static shared-token authentication for this NetBrain POC (not OAuth)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            authorized = False
            if len(headers) == 1:
                scheme, _, token = headers[0].partition(b" ")
                authorized = (scheme.lower() == b"bearer" and
                              secrets.compare_digest(token, MCP_API_TOKEN.encode("ascii")))
            if not authorized:
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# Wrapping the whole app protects discovery and calls, and preserves MCP lifespan.
app = BearerTokenMiddleware(mcp.streamable_http_app())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
