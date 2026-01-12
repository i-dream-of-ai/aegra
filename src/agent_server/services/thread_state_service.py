"""Thread state conversion service"""

from datetime import datetime
from typing import Any

import structlog

from ..core.serializers import LangGraphSerializer
from ..models.threads import ThreadCheckpoint, ThreadState

logger = structlog.getLogger(__name__)


def _patch_dangling_tool_calls(messages: list[Any]) -> list[Any]:
    """Patch dangling tool calls in messages list.

    This handles the case where an AI message has tool_calls but there's no
    corresponding ToolMessage with the matching tool_call_id. This can happen
    when a run is interrupted mid-tool-call.

    We add synthetic ToolMessages for any dangling tool calls so the message
    history is valid for LLM APIs that require tool_call/tool_result pairing.
    """
    if not messages:
        return messages

    # Build set of existing tool result IDs
    tool_result_ids = set()
    for msg in messages:
        # Handle both dict and object forms
        if isinstance(msg, dict):
            if msg.get("type") == "tool" and msg.get("tool_call_id"):
                tool_result_ids.add(msg["tool_call_id"])
        elif hasattr(msg, "type") and getattr(msg, "type", None) == "tool":
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                tool_result_ids.add(tool_call_id)

    # Check if any AI messages have dangling tool calls
    patched = []
    for msg in messages:
        patched.append(msg)

        # Get tool_calls from message (dict or object)
        tool_calls = None
        msg_type = None
        if isinstance(msg, dict):
            tool_calls = msg.get("tool_calls")
            msg_type = msg.get("type")
        else:
            tool_calls = getattr(msg, "tool_calls", None)
            msg_type = getattr(msg, "type", None)

        if msg_type == "ai" and tool_calls:
            for tc in tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")

                if tc_id and tc_id not in tool_result_ids:
                    # Add synthetic tool result
                    logger.warning(
                        "Patching dangling tool call in snapshot",
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                    )
                    patched.append({
                        "type": "tool",
                        "name": tc_name,
                        "tool_call_id": tc_id,
                        "content": f"Tool {tc_name} was interrupted/cancelled.",
                    })
                    tool_result_ids.add(tc_id)  # Don't double-patch

    return patched


class ThreadStateService:
    """Service for converting LangGraph snapshots to ThreadState objects"""

    def __init__(self) -> None:
        self.serializer = LangGraphSerializer()

    def convert_snapshot_to_thread_state(
        self, snapshot: Any, thread_id: str, subgraphs: bool = False
    ) -> ThreadState:
        """Convert a LangGraph snapshot to ThreadState format"""
        try:
            # Extract basic values
            values = getattr(snapshot, "values", {})
            next_nodes = getattr(snapshot, "next", []) or []
            metadata = getattr(snapshot, "metadata", {}) or {}
            created_at = self._extract_created_at(snapshot)

            # Patch dangling tool calls in messages to prevent API errors
            if isinstance(values, dict) and "messages" in values:
                values = dict(values)  # Make a copy to avoid mutating original
                values["messages"] = _patch_dangling_tool_calls(values["messages"])

            # Debug: Log the keys in values to see if 'ui' is present
            logger.debug(
                "Snapshot values keys",
                thread_id=thread_id,
                value_keys=list(values.keys()) if isinstance(values, dict) else "not_a_dict",
                has_ui="ui" in values if isinstance(values, dict) else False,
                ui_count=len(values.get("ui", [])) if isinstance(values, dict) else 0,
            )

            # Extract tasks and interrupts using serializer
            tasks = self.serializer.extract_tasks_from_snapshot(snapshot)

            # Recursively serialize tasks' state (which might be subgraphs)
            if subgraphs:
                for task in tasks:
                    if "state" in task and task["state"] is not None:
                        try:
                            task["state"] = self.convert_snapshot_to_thread_state(
                                task["state"], thread_id, subgraphs=True
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to serialize subgraph state for task {task.get('id')}: {e}"
                            )
                            task["state"] = None

            interrupts = self.serializer.extract_interrupts_from_snapshot(snapshot)

            # Create checkpoint objects
            current_checkpoint = self._create_checkpoint(snapshot.config, thread_id)
            parent_checkpoint = (
                self._create_checkpoint(snapshot.parent_config, thread_id)
                if snapshot.parent_config
                else None
            )

            # Extract checkpoint IDs for backward compatibility
            checkpoint_id = self._extract_checkpoint_id(snapshot.config)
            parent_checkpoint_id = (
                self._extract_checkpoint_id(snapshot.parent_config)
                if snapshot.parent_config
                else None
            )

            return ThreadState(
                values=values,
                next=next_nodes,
                tasks=tasks,
                interrupts=interrupts,
                metadata=metadata,
                created_at=created_at,
                checkpoint=current_checkpoint,
                parent_checkpoint=parent_checkpoint,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=parent_checkpoint_id,
            )

        except Exception as e:
            logger.error(
                f"Failed to convert snapshot to thread state: {e} "
                f"(thread_id={thread_id}, snapshot_type={type(snapshot).__name__})"
            )
            raise

    def convert_snapshots_to_thread_states(
        self, snapshots: list[Any], thread_id: str
    ) -> list[ThreadState]:
        """Convert multiple snapshots to ThreadState objects"""
        thread_states = []

        for i, snapshot in enumerate(snapshots):
            try:
                thread_state = self.convert_snapshot_to_thread_state(
                    snapshot, thread_id
                )
                thread_states.append(thread_state)
            except Exception as e:
                logger.error(
                    f"Failed to convert snapshot in batch: {e} "
                    f"(thread_id={thread_id}, snapshot_index={i})"
                )
                # Continue with other snapshots rather than failing the entire batch
                continue

        return thread_states

    def _extract_created_at(self, snapshot: Any) -> datetime | None:
        """Extract created_at timestamp from snapshot"""
        created_at = getattr(snapshot, "created_at", None)
        if isinstance(created_at, str):
            try:
                return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                logger.warning(f"Invalid created_at format: {created_at}")
                return None
        elif isinstance(created_at, datetime):
            return created_at
        return None

    def _create_checkpoint(self, config: Any, thread_id: str) -> ThreadCheckpoint:
        """Create ThreadCheckpoint from config"""
        if not config or not isinstance(config, dict):
            return ThreadCheckpoint(
                checkpoint_id=None, thread_id=thread_id, checkpoint_ns=""
            )

        configurable = config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id")
        checkpoint_ns = configurable.get("checkpoint_ns", "")

        return ThreadCheckpoint(
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )

    def _extract_checkpoint_id(self, config: Any) -> str | None:
        """Extract checkpoint ID from config for backward compatibility"""
        if not config or not isinstance(config, dict):
            return None

        configurable = config.get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id")
        return str(checkpoint_id) if checkpoint_id is not None else None
