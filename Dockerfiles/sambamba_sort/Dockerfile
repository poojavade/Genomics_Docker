FROM ubuntu:16.04
 
COPY sambamba/ /usr/local/sambamba_sort/sambamba/v0.5.4/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/sambamba_sort/sambamba/v0.5.4:$PATH"
