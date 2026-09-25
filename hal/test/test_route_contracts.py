"""Public route declarations must retain their request/response contracts.

Use the AST so CI can check declarations without importing hardware drivers.
This catches the historical /emotion decorator landing on a zero-argument
helper, which is valid Python and therefore passes a syntax-only gate.
"""

import ast
from pathlib import Path
import unittest


def emotion_endpoints(source):
    matches = []
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "router"
                and decorator.func.attr == "post"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == "/emotion"
            ):
                matches.append((node, decorator))
    return matches


class EmotionRouteContractTests(unittest.TestCase):
    def assert_emotion_contract(self, source):
        routes = emotion_endpoints(source)
        self.assertEqual(len(routes), 1, "POST /emotion must be registered exactly once")
        handler, decorator = routes[0]
        required_count = len(handler.args.args) - len(handler.args.defaults)
        required = handler.args.args[:required_count]
        self.assertTrue(
            any(isinstance(arg.annotation, ast.Name) and arg.annotation.id == "EmotionRequest"
                for arg in required),
            "POST /emotion must require an EmotionRequest body, not a helper signature",
        )
        self.assertTrue(any(
            keyword.arg == "response_model"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "EmotionResponse"
            for keyword in decorator.keywords
        ), "POST /emotion must return EmotionResponse")

    def test_registered_emotion_request_and_response(self):
        path = Path(__file__).resolve().parents[1] / "routes" / "emotion.py"
        self.assert_emotion_contract(path.read_text())

    def test_rejects_decorator_accidentally_attached_to_helper(self):
        source = '''
@router.post("/emotion", response_model=EmotionResponse)
def harness_blocks_sleep() -> bool:
    return False

def express_emotion(req: EmotionRequest, source: str = "api"):
    pass
'''
        compile(source, "misplaced-decorator", "exec")
        with self.assertRaisesRegex(AssertionError, "EmotionRequest body"):
            self.assert_emotion_contract(source)


if __name__ == "__main__":
    unittest.main()
