targetScope = 'resourceGroup'

param location string = resourceGroup().location
param jobName string = 'ai-factory-coder'
param foundryAccountName string
param foundryProjectName string
param logAnalyticsWorkspaceName string
param toolboxVersion string = '1'
param skillPolicy object
param changeRequest object
param model string = 'claude-haiku-4.5'
@description('Deploy supporting resources first, then supply an immutable image digest and enable the job.')
param deployJob bool = false
param image string = ''
@description('Requires a Copilot credential already provisioned as copilot-token in the output Key Vault.')
param enableLive bool = false

var suffix = uniqueString(resourceGroup().id, jobName)
var registryName = 'afcoder${suffix}'
var storageName = 'afcoder${suffix}'
var vaultName = 'afcoder-${suffix}'
var artifactShare = 'artifacts'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${jobName}-identity'
  location: location
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    accessPolicies: []
    publicNetworkAccess: 'Enabled'
  }
}

resource vaultRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, 'KeyVaultSecretsUser')
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource foundryRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, identity.id, 'FoundryUser')
  scope: foundryProject
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '53ca6127-db72-4b80-b1b0-d745d6d5456d'
    )
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-08-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    azureFilesIdentityBasedAuthentication: {
      directoryServiceOptions: 'None'
      smbOAuthSettings: {
        isSmbOAuthEnabled: true
      }
    }
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Enabled'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: artifactShare
  properties: {
    shareQuota: 10
    enabledProtocols: 'SMB'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-10-02-preview' = {
  name: '${jobName}-env'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource storageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, 'StorageFileDataSmbMiAdmin')
  scope: storage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'a235d3ee-5935-4cfb-8cc5-a3303ad5995e'
    )
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2026-03-02-preview' = {
  parent: environment
  name: artifactShare
  properties: {
    azureFile: {
      accountName: storage.name
      identity: identity.id
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [
    storageAccess
  ]
}

module worker 'main.bicep' = if (deployJob) {
  name: '${jobName}-worker'
  params: {
    location: location
    jobName: jobName
    environmentId: environment.id
    identityId: identity.id
    identityClientId: identity.properties.clientId
    image: image
    acrLoginServer: registry.properties.loginServer
    artifactStorageName: environmentStorage.name
    copilotSecretUrl: enableLive ? '${vault.properties.vaultUri}secrets/copilot-token' : ''
    sourceConfig: {
      kind: 'foundry'
      skill_mode: 'provider'
      binding: {
        roles: {
          coder: {
            project_endpoint: 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/${foundryProject.name}'
            toolbox: 'coder-skills'
            version: toolboxVersion
            identity: {
              type: 'managed_identity'
              tenant_id: subscription().tenantId
              client_id: identity.properties.clientId
              principal_id: identity.properties.principalId
            }
          }
        }
      }
    }
    skillPolicy: skillPolicy
    changeRequest: changeRequest
    model: model
    roles: ['coder']
    tasks: ['implementation']
    enableLive: enableLive
  }
  dependsOn: [
    registryPull
    vaultRead
    foundryRead
  ]
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output storageAccountName string = storage.name
output artifactShareName string = share.name
output keyVaultName string = vault.name
output environmentName string = environment.name
output identityResourceId string = identity.id
output identityPrincipalId string = identity.properties.principalId
output jobName string = deployJob ? jobName : ''
