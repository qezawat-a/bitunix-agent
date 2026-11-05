"""Setup script for jrock package (fallback for platforms that need setup.py)."""
from setuptools import setup, find_packages

setup(
    name="jrock",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "numpy>=1.26.0",
        "pandas>=2.0.0",
        "httpx>=0.27.0",
        "python-telegram-bot>=21.0",
        "websockets>=12.0",
        "python-dotenv>=1.0.0",
        "pyyaml>=6.0.0",
        "rich>=13.0.0",
        "textual>=0.60.0",
        "pydantic>=2.0.0",
        "aiohttp>=3.9.0",
        "markdownify>=0.11.0",
    ],
    entry_points={
        "console_scripts": [
            "jrock = jrock.main:main",
        ],
    },
)