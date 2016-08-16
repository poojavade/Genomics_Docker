FROM ubuntu:16.04
 
COPY bwa/ /usr/local/bwa_wrapper_condor/bwa/0.7.12/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/bwa_wrapper_condor/bwa/0.7.12:$PATH"
