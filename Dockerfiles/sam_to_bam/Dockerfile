FROM ubuntu:16.04
 
COPY samtools/ /usr/local/sam_to_bam/samtools/1.2/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/sam_to_bam/samtools/1.2/bin:$PATH"
