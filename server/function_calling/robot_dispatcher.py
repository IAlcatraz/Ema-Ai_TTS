"""
Dispatcher for the Robot_Patreon 3D system.
Translates LLM tool calls into API requests for the robot controller.
"""

import requests
import json
from server.config import ROBOT_API_URL, vlog

def dispatch_robot_tool(tool_name: str, arguments: dict) -> str:
    """
    Sends a physical command to the Robot_Patreon system.

    Args:
        tool_name: The name of the tool from tool_calls.json (e.g., 'Move')
        arguments: The parameters for the tool (e.g., {'direction': 'forward'})

    Returns:
        A string describing the outcome of the action for the LLM history.
    """
    vlog(f"[robot_disp] executing {tool_name} with args={arguments}")

    try:
        # We assume the robot controller has an endpoint for tool execution.
        # If Robot_Patreon uses a different protocol (e.g. WebSocket),
        # this logic will be updated to match.
        payload = {
            "tool": tool_name,
            "args": arguments
        }

        # We use a hypothetical /execute endpoint.
        # Based on Robot_Patreon's structure, this might be a POST request.
        response = requests.post(
            f"{ROBOT_API_URL}/api/execute",
            json=payload,
            timeout=5
        )

        if response.status_code == 200:
            vlog(f"[robot_disp] {tool_name} executed successfully")
            return f"Successfully executed {tool_name} with {arguments}."
        else:
            vlog(f"[robot_disp] {tool_name} failed with status {response.status_code}")
            return f"Failed to execute {tool_name}. Server returned {response.status_code}."

    except Exception as e:
        vlog(f"[robot_disp] Error dispatching {tool_name}: {e}")
        return f"Error executing {tool_name}: {str(e)}"
