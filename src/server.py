from workbench.worker import *


class UpperTool(Tool):
    def __init__(self):
        super().__init__("upper", "Uppercase a string.",
                         [Parameter("text", "string", "Text to uppercase")])

    def execute(self, text: str) -> str:  # sync is fine too
        return text.upper()


if __name__ == "__main__":
    agent = AgenticWorker(
        name="Echo Agent",
        llm=AnthropicProvider(),
        model="xxx",
        description="Echoes, uppercases, and can run shell commands.",
        skills=[Skill(id="echo", name="Echo", description="Repeats input", tags=["demo"])],
        tools=[UpperTool(), BashTool(timeout=10, allowed_commands=["ls", "echo", "date"]),
               ListParticipantsTool(), DelegateTool(),
               BoardChannelsTool(), BoardPostTool(), BoardReadTool(), BoardWaitTool()],
    )
    agent.run(host="127.0.0.1", port=9999, admin_password="1234")  # password from ADMIN_PASSWORD env