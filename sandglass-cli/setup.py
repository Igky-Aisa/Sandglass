from setuptools import setup, find_packages

setup(
    name="sandglass",
    version="0.10.0",
    description="Batch prompt queue executor for Claude",
    packages=find_packages(exclude=["tests", "tests.*"]),
    include_package_data=True,
    package_data={"sandglass": ["templates/*.md", "templates/*/*.md", "assets/*.jpg"]},
    install_requires=[
        "typer>=0.9.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "sandglass=sandglass.cli:app",
        ],
    },
    python_requires=">=3.9",
)
