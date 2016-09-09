#!/usr/bin/env cwl-runner

class: CommandLineTool
id: BWA_MEM
label: BWA_MEM tool
cwlVersion: latest

dct:creator:
  foaf:name: Pooja Vade
  foaf:mbox: pooja043@uw.edu

requirements:
- class: DockerRequirement
  dockerPull: quay.io/pooja043/genomics_docker:latest

hints:
- class: ResourceRequirement
  coresMin: 1
  ramMin: 4092
  outdirMin: 512000
  description: the process requires at least 4G of RAM

inputs:
  fastqsanger_reverse_input:
    type: File
    format: http://edamontology.org/format_1932
    inputBinding:
      position: 2
    doc: The fastqsanger reverse file used as input.

  fastqsanger_forward_input:
    type: File
    format: http://edamontology.org/format_1932
    inputBinding:
      position: 1
    doc: The fastqsanger forward file used as input.

outputs:
  bwamem_result:
    type: File
    format: http://edamontology.org/format_2572
    outputBinding:
      glob: bwamem_result
    doc: result of bwamem
    
baseCommand: [bash, /usr/local/bin/bwa]

