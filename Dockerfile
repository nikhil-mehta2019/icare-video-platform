# Use the official Python lightweight image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy your requirements file first (this caches dependencies to make future builds faster)
COPY requirements.txt .

# Install the Python libraries
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your project files into the container
COPY . .

# Expose the port FastAPI runs on
EXPOSE 8000

# Command to boot up the Uvicorn server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]