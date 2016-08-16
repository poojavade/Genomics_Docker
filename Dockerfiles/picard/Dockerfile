FROM ubuntu:16.04

COPY default/ /usr/local/picard/


RUN apt-get update && \
    apt-get install -y default-jdk && \
    apt-get clean

RUN \
  apt-get update && \
  apt-get install -y python


ENV JAVA_JAR_PATH "/usr/local/picard/"