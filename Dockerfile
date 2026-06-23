FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY app/ app/

# Install dependencies
RUN uv pip install --system .

# Expose port for FastAPI
EXPOSE 8080

# Run the FastAPI app
CMD ["python", "-m", "app.fast_api_app"]
