from setuptools import find_packages, setup


def get_requirements():
    with open("requirements.txt") as fp:
        return [x.strip() for x in fp.read().split("\n") if not x.startswith("#")]


with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="agentic",
    version="0.1.0",
    author="getsentry",
    author_email="jenn@sentry.io",
    description="A Python library for building powerful LLM-based agents",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/getsentry/agentic",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.8",
    install_requires=get_requirements(),
)
