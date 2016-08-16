FROM ubuntu:16.04
 
COPY samtools/ /usr/local/bam_to_sam/samtools/1.2/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/bam_to_sam/samtools/1.2/bin:$PATH"
