FROM python:3.14

RUN mkdir /app/
WORKDIR /app

COPY src/MarketService src/MarketService
COPY pyproject.toml ./
COPY README.md ./
RUN pip install ./

ENTRYPOINT ["python3", "src/MarketService/market_service.py"]