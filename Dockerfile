ARG SYNAPSE_IMAGE
FROM ${SYNAPSE_IMAGE}
COPY . /opt/synapse-space-autojoin
RUN /usr/local/bin/python -m pip install --no-deps /opt/synapse-space-autojoin
