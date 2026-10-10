"""Unit tests for schema validation."""
from datetime import UTC, datetime

from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    generate_chat_id,
)


class TestChatCompletionRequest:
    """Test cases for ChatCompletionRequest schema."""

    def test_valid_request(self):
        """Test valid request creation."""
        request = ChatCompletionRequest(
            model="gpt-3.5-turbo",
            messages=[
                ChatMessage(role="user", content="Hello"),
                ChatMessage(role="assistant", content="Hi there"),
            ],
            max_tokens=100,
        )

        assert request.model == "gpt-3.5-turbo"
        assert len(request.messages) == 2
        assert request.max_tokens == 100

    def test_minimal_request(self):
        """Test minimal valid request."""
        request = ChatCompletionRequest(
            model="gpt-3.5-turbo",
            messages=[ChatMessage(role="user", content="Hello")],
        )

        assert request.model == "gpt-3.5-turbo"
        assert len(request.messages) == 1
        assert request.max_tokens is None

    def test_extra_parameters(self):
        """Test request with extra parameters."""
        request = ChatCompletionRequest(
            model="gpt-3.5-turbo",
            messages=[ChatMessage(role="user", content="Hello")],
            temperature=0.5,
            top_p=0.9,
            frequency_penalty=0.2,
            presence_penalty=0.1,
            stream=False,
            stop=["\n"],
            n=1,
            user="user-123",
        )

        assert request.temperature == 0.5
        assert request.top_p == 0.9
        assert request.frequency_penalty == 0.2
        assert request.presence_penalty == 0.1
        assert request.stop == ["\n"]
        assert request.n == 1


class TestChatCompletionResponse:
    """Test cases for ChatCompletionResponse schema."""

    def test_response_creation(self):
        """Test response creation."""
        response = ChatCompletionResponse(
            id="chatcmpl-123",
            object="chat.completion",
            created=int(datetime.now(UTC).timestamp()),
            model="gpt-3.5-turbo",
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Hello!"),
                    finish_reason="stop",
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
            ),
        )

        assert response.id == "chatcmpl-123"
        assert response.object == "chat.completion"
        assert len(response.choices) == 1
        assert response.usage.total_tokens == 30


class TestChatCompletionChoice:
    """Test cases for ChatCompletionChoice schema."""

    def test_choice_creation(self):
        """Test choice creation."""
        choice = ChatCompletionChoice(
            index=0,
            message=ChatMessage(role="assistant", content="Hello!"),
            finish_reason="stop",
        )

        assert choice.index == 0
        assert choice.finish_reason == "stop"


class TestChatMessage:
    """Test cases for ChatMessage schema."""

    def test_message_creation(self):
        """Test message creation."""
        message = ChatMessage(role="user", content="Hello!")

        assert message.role == "user"
        assert message.content == "Hello!"

    def test_message_with_name(self):
        """Test message with name."""
        message = ChatMessage(role="user", content="Hello!", name="assistant")

        assert message.role == "user"
        assert message.content == "Hello!"
        assert message.name == "assistant"


class TestChatCompletionUsage:
    """Test cases for ChatCompletionUsage schema."""

    def test_usage_creation(self):
        """Test usage creation."""
        usage = ChatCompletionUsage(
            prompt_tokens=100,
            completion_tokens=200,
            total_tokens=300,
        )

        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 200
        assert usage.total_tokens == 300


class TestGenerateChatId:
    """Test cases for generate_chat_id function."""

    def test_id_format(self):
        """Test that ID is properly formatted."""
        chat_id = generate_chat_id()

        assert isinstance(chat_id, str)
        assert len(chat_id) > 0
        assert chat_id.startswith("chatcmpl-")
