FROM ubuntu:16.04
 
COPY 0.11.3/ /usr/local/FastQC/
 
RUN apt-get update && \
    apt-get install -y default-jdk && \
    apt-get clean

RUN \
  apt-get update && \
  apt-get install -y python

ENV FASTQC_JAR_PATH "/usr/local/FastQC/" 
ENV PATH "$FASTQC_JAR_PATH:$PATH"
