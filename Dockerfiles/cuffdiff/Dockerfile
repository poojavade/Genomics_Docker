FROM ubuntu:16.04
 
COPY diff_default/ /usr/local/cufflinks/
 
RUN \
  apt-get update && \
  apt-get install -y python

 
ENV PATH /usr/local/cufflinks:$PATH"


