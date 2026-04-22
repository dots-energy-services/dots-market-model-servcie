FROM python:3.14

RUN mkdir /app/
WORKDIR /app

COPY src/MarketModelService src/MarketModelService
COPY pyproject.toml ./
COPY README.md ./
RUN pip install ./

ENTRYPOINT ["python3", "src/MarketModelService/market_service.py"]