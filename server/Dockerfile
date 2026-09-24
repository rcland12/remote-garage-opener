# The image is one file plus one dependency, so it stays small and there is
# very little in it to have a vulnerability. PyJWT is only needed for
# AUTH_MODE=access; it is installed unconditionally so one image covers both
# modes.
FROM python:3.13-slim

ARG USER=garage
ARG USER_ID=1000
ARG GROUP_ID=1000

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g ${GROUP_ID} ${USER} && \
    useradd -u ${USER_ID} -g ${GROUP_ID} -m ${USER}

RUN pip install --no-cache-dir 'PyJWT[crypto]==2.10.1'

COPY ./app.py /app/app.py

RUN chown -R ${USER}:${USER} /app

ENV PYTHONUNBUFFERED=1

USER ${USER}

EXPOSE 8080

CMD ["python", "/app/app.py"]
