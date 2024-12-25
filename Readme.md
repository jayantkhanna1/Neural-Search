
# Neural Search Application

## Overview
The Neural Search Application is a Django-based web application that uses AI to crawl the internet in the background and retrieve links related to a user query. It leverages neural search algorithms to provide highly relevant results based on a specified matching threshold.

## Features
- **AI-Powered Search**: Uses neural networks to analyze and match query intent.
- **Background Crawling**: Performs internet crawling as a background job for scalability.
- **Custom Threshold Filtering**: Users can set a matching threshold for result relevance.
- **Asynchronous Processing**: Ensures smooth user experience with background task handling.

## Requirements
- Python 3.x
- Django 4.x
- Celery
- Redis
- Requests
- BeautifulSoup4
- Django Channels

## Installation
1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd neural-search-app
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure environment variables in `.env`:
   ```plaintext
   DJANGO_SECRET_KEY=<your-secret-key>
   REDIS_URL=redis://localhost:6379/0
   ```
4. Run migrations:
   ```bash
   python manage.py migrate
   ```
5. Start Redis server:
   ```bash
   redis-server
   ```
6. Start Celery worker:
   ```bash
   celery -A app_name worker --loglevel=info
   ```
7. Start the server:
   ```bash
   python manage.py runserver
   ```

## Usage
1. Open the application in your browser:
   ```
   http://127.0.0.1:8000/
   ```
2. Enter your query and matching threshold.
3. Submit the query to initiate the search.
4. View results once the background job is complete.

## API Endpoints
- **POST /search**: Submits a search query.
  - Parameters:
    - `query` (string): The search term.
    - `threshold` (float): Matching relevance threshold.
- **GET /results/{query_id}**: Fetches results for a specific query.

## Contributing
1. Fork the repository.
2. Create a new branch:
   ```bash
   git checkout -b feature-branch
   ```
3. Commit changes and push:
   ```bash
   git commit -m "Add new feature"
   git push origin feature-branch
   ```
4. Submit a pull request.

