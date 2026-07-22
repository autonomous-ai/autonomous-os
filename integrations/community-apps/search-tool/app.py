"""Gradio MCP app for the Autonomous OS Search Tool Space."""

import gradio as gr
from search_tool import search_web


TOOL_DESCRIPTION = (
    "Search the web for current information and return a short list of results (title, snippet, url). "
    "Call this directly whenever the user asks to search, check the web, look something up, find today's events, "
    "or learn what is happening now. Do not just say you'll look it up."
)


with gr.Blocks(title="Autonomous OS Search Tool") as demo:
    query = gr.Textbox(label="Query", placeholder="today's robotics news")
    max_results = gr.Slider(label="Max results", minimum=1, maximum=10, step=1, value=5)
    search_button = gr.Button("Search", variant="primary")
    output = gr.JSON(label="Results")

    search_button.click(
        search_web,
        inputs=[query, max_results],
        outputs=output,
        api_name=False,
        queue=False,
    )

    gr.api(
        search_web,
        api_name="search_web",
        api_description=TOOL_DESCRIPTION,
        queue=False,
        concurrency_limit=None,
    )


if __name__ == "__main__":
    demo.launch(mcp_server=True)
