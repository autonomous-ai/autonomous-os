from pydantic import BaseModel


class DelegateSignal(BaseModel):
    """Yielded by stream_output() when the model calls delegate_to_main."""

    message: str = ""
