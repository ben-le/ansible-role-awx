# Ansible role: AWX 1.0.1
An Ansible role that installs AWX 1.0.1 on Debian 9 and Ubuntu 16.x

Requirements
--
If you want to deployment NGINX/SSL, you need to download ben-le.awx_nginx role as well

Role Variables
--
Please review or change the default variables in the default/main.yml

Example Playbook

- hosts: awx
  roles:
    - lae.docker
    - ben-le.awx
    - ben-le.awx_nginx


