#!/usr/bin/env python3
"""
Server entry point for Newsletter Advertiser Intelligence System.

This is the main entry point for cloud deployment (Railway, Render, etc.)
"""

import os
import logging
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def main():
    """Start the web server."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))

    logger.info(f"Starting Newsletter Advertiser Intelligence System on {host}:{port}")

    uvicorn.run(
        "src.web.app:app",
        host=host,
        port=port,
        log_level="info",
        # Reload disabled in production
        reload=os.getenv("ENVIRONMENT", "production") == "development",
    )


if __name__ == "__main__":
    main()
