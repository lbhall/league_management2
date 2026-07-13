pipeline {
    agent any

    parameters {
        choice(
            name: 'DEPLOY_TYPE',
            choices: ['update', 'fresh-install'],
            description: 'update: pull latest code and restart. fresh-install: clone repo, create venv, migrate, collectstatic.'
        )
        string(
            name: 'REPO_URL',
            defaultValue: 'git@github.com:your-org/league_management2.git',
            description: 'Git repo URL (only used for fresh-install).'
        )
    }

    environment {
        PROD_HOST       = 'emcfunleague.com'
        PROD_USER       = 'bhall'
        PROD_SSH_PORT   = '56'
        SOURCE_DIR      = '/var/www/emcfunleague.com/source'
        VENV_DIR        = '/var/www/emcfunleague.com/venv'
        PYTHON          = '/usr/bin/python3'
    }

    stages {
        stage('Deploy') {
            steps {
                sshagent(credentials: ['prod-ssh-key']) {
                    script {
                        def ssh = "ssh -p ${env.PROD_SSH_PORT} -o StrictHostKeyChecking=no ${env.PROD_USER}@${env.PROD_HOST}"

                        if (params.DEPLOY_TYPE == 'update') {
                            sh """
                                ${ssh} 'bash ${env.SOURCE_DIR}/deploy.sh'
                            """
                        } else {
                            sh """
                                ${ssh} '
                                    set -e

                                    # Clone repo if source dir does not exist
                                    if [ ! -d "${env.SOURCE_DIR}/.git" ]; then
                                        sudo mkdir -p ${env.SOURCE_DIR}
                                        sudo chown ${env.PROD_USER}:${env.PROD_USER} ${env.SOURCE_DIR}
                                        git clone ${params.REPO_URL} ${env.SOURCE_DIR}
                                    else
                                        git -C ${env.SOURCE_DIR} pull
                                    fi

                                    # Create virtualenv if it does not exist
                                    if [ ! -d "${env.VENV_DIR}" ]; then
                                        ${env.PYTHON} -m venv ${env.VENV_DIR}
                                    fi

                                    # Install dependencies
                                    ${env.VENV_DIR}/bin/pip install --quiet -r ${env.SOURCE_DIR}/requirements.txt

                                    # Run migrations
                                    ${env.VENV_DIR}/bin/python ${env.SOURCE_DIR}/manage.py migrate --noinput

                                    # Collect static files
                                    ${env.VENV_DIR}/bin/python ${env.SOURCE_DIR}/manage.py collectstatic --noinput

                                    # Restart gunicorn if service exists
                                    if systemctl list-units --type=service | grep -q gunicorn.service; then
                                        sudo systemctl restart gunicorn
                                    else
                                        echo "gunicorn.service not found — configure systemd units manually."
                                    fi
                                '
                            """
                        }
                    }
                }
            }
        }
    }

    post {
        success {
            echo "Deployment (${params.DEPLOY_TYPE}) completed successfully."
        }
        failure {
            echo "Deployment (${params.DEPLOY_TYPE}) failed."
        }
    }
}
