"""Manual pass-through companion agent for pasted assistant replies."""

import sys

from sandbox.agents.base_agent import BaseAgent


class ManualCompanionAgent(BaseAgent):
    """Print the simulated user message and read a pasted assistant reply."""

    END_MARKER = "::end"

    def __init__(self, name=None, config=None, input_func=None, output_stream=None):
        BaseAgent.__init__(self, name=name, config=config)
        self.input_func = input_func or input
        self.output_stream = output_stream or sys.stdout

    def generate(self, context):
        """Collect a multi-line pasted reply terminated by ::end."""
        self.validate_context(context, ["visible_history", "current_user_message"])

        memory = context.get("optional_memory", {}) or {}
        turn_id = memory.get("turn_id", len(context.get("visible_history", [])) + 1)
        self._write("\n" + "=" * 72)
        self._write("Manual companion turn: %s" % turn_id)
        self._write("Copy this user message into the tested model:")
        self._write("-" * 72)
        self._write(str(context.get("current_user_message", "")))
        self._write("-" * 72)
        self._write("Paste the full assistant reply below.")
        self._write("Finish with a new line containing %s and press Enter:" % self.END_MARKER)

        lines = []
        while True:
            try:
                line = self.input_func()
            except EOFError:
                if lines:
                    break
                raise ValueError("Manual companion input ended before a response was provided.")
            if line.strip() == self.END_MARKER:
                break
            lines.append(line)

        assistant_message = "\n".join(lines).strip()
        if not assistant_message:
            raise ValueError("Manual companion response cannot be empty.")

        return {
            "assistant_message": assistant_message,
            "metadata": {
                "agent_type": "manual_companion",
                "input_method": "terminal_paste",
                "used_hidden_state": False,
                "used_private_fields": False,
            },
        }

    def _write(self, text):
        self.output_stream.write(str(text) + "\n")
        self.output_stream.flush()
