import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.compose.compiler)
}

val redirectHost = providers.gradleProperty("mobileRedirectHost").orElse("panel.invalid").get()
require(redirectHost.matches(Regex("[A-Za-z0-9.-]+")))
val privateLan = providers.gradleProperty("mobilePrivateLan").orElse("false").get().toBooleanStrict()
val persistentLan = providers.gradleProperty("mobilePersistentLanSession").orElse("false").get().toBooleanStrict()
require(!persistentLan || privateLan)
val redirectUri = if (privateLan) "ru.maxpostiz.panel:/oauth2redirect"
    else "https://$redirectHost/android/oidc-return"

abstract class GenerateLanCa : DefaultTask() {
    @get:InputFile
    abstract val caFile: RegularFileProperty

    @get:OutputDirectory
    abstract val outputDirectory: DirectoryProperty

    @TaskAction
    fun generate() {
        val source = caFile.get().asFile
        val pem = source.readText(Charsets.US_ASCII).trim()
        require(Regex("-----BEGIN CERTIFICATE-----[\\r\\n]+[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----")
            .matches(pem)) { "Use a PEM file containing exactly one public root CA certificate." }
        val certificate = source.inputStream().use {
            CertificateFactory.getInstance("X.509").generateCertificate(it) as X509Certificate
        }
        certificate.checkValidity()
        require(certificate.basicConstraints >= 0 &&
            certificate.subjectX500Principal == certificate.issuerX500Principal) {
            "The LAN trust anchor must be a root CA certificate."
        }
        certificate.verify(certificate.publicKey)
        val resource = outputDirectory.file("raw/mobile_private_ca.crt").get().asFile
        resource.parentFile.mkdirs()
        resource.writeBytes(certificate.encoded)
    }
}

android {
    namespace = "ru.maxpostiz.panel"
    compileSdk = 37

    defaultConfig {
        applicationId = "ru.maxpostiz.panel"
        minSdk = 33
        targetSdk = 37
        versionCode = 9
        versionName = "0.1.8"
        manifestPlaceholders["appAuthRedirectScheme"] = "https"
        manifestPlaceholders["mobileRedirectHost"] = redirectHost
        buildConfigField("String", "REDIRECT_URI", "\"$redirectUri\"")
        buildConfigField("boolean", "PRIVATE_LAN", privateLan.toString())
        buildConfigField("boolean", "PERSISTENT_LAN_SESSION", persistentLan.toString())
    }
    buildFeatures {
        buildConfig = true
        compose = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
        }
    }
    if (privateLan) {
        sourceSets.getByName("release") {
            manifest.srcFile("src/privateLan/AndroidManifest.xml")
            res.directories.add("src/privateLan/res")
        }
    }
}

if (privateLan) {
    val caPath = providers.gradleProperty("mobilePrivateCa")
    require(caPath.isPresent) { "Set -PmobilePrivateCa to the public LAN root CA PEM file." }
    val generateLanCa = tasks.register<GenerateLanCa>("generateLanCa") {
        caFile.set(layout.file(caPath.map { rootProject.file(it) }))
        outputDirectory.set(layout.buildDirectory.dir("generated/lan-ca/res"))
    }
    androidComponents {
        beforeVariants(selector().withBuildType("debug")) { it.enable = false }
        onVariants(selector().withBuildType("release")) { variant ->
            variant.sources.res?.addGeneratedSourceDirectory(generateLanCa, GenerateLanCa::outputDirectory)
        }
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.foundation)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.viewmodel)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.appauth)
    testImplementation(libs.junit)
    testImplementation(libs.json.test)
}
